#2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Handles all requests relating to volumes.
"""


import collections
import datetime
import functools
import json

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
from oslo_utils import encodeutils
import six

from cinder import context
from cinder import db 
from cinder.db import base
from cinder import exception
from cinder import flow_utils
from cinder.i18n import _, _LE, _LI, _LW
from cinder.image import glance
from cinder import keymgr
from cinder import objects
from cinder.objects import base as objects_base
import cinder.policy
from cinder import quota
from cinder import quota_utils
from cinder.scheduler import rpcapi as scheduler_rpcapi
from cinder import utils
from cinder.volume.flows.api import create_volume
from cinder.volume import qos_specs
from cinder.volume import rpcapi as volume_rpcapi
from cinder.volume import utils as volume_utils
from cinder.volume import volume_types
from cinder.api.metricutil import ReportMetrics
from sbsvolumeworkflowclient.cinderasyncvolumeclient import CinderAsyncVolumeClient

volume_host_opt = cfg.BoolOpt('snapshot_same_host',
                              default=True,
                              help='Create volume from snapshot at the host '
                                   'where snapshot resides')
volume_same_az_opt = cfg.BoolOpt('cloned_volume_same_az',
                                 default=True,
                                 help='Ensure that the new volumes are the '
                                      'same AZ as snapshot or source volume')
az_cache_time_opt = cfg.IntOpt('az_cache_duration',
                               default=3600,
                               help='Cache volume availability zones in '
                                    'memory for the provided duration in '
                                    'seconds')
sbs_volume_async_host = cfg.StrOpt('sbs-volume-async-host',
                                   default="sbsvolumeasynchost",
                                   help='Host of SbsVolumeAsyncService')
sbs_volume_async_port = cfg.IntOpt('sbs-volume-async-port',
                                   default=9080,
                                   help='Port of SbsVolumeAsyncService')
create_volume_new_customer_whitelist = cfg.StrOpt('create_volume_new_customer_whitelist',
                                                  default="",
                                                  help='List of whitelisted customers for new create workflow')
delete_volume_new_customer_whitelist = cfg.StrOpt('delete_volume_new_customer_whitelist',
                                                  default="",
                                                  help='List of whitelisted customers for new delete workflow')

rbd_opts = [
    cfg.StrOpt('rbd_pool',
               default='rbd',
               help='The RADOS pool where rbd volumes are stored'),
    cfg.StrOpt('rbd_user',
               default=None,
               help='The RADOS client name for accessing rbd volumes '
                    '- only set when using cephx authentication'),
    cfg.StrOpt('rbd_ceph_conf',
               default='',  # default determined by librados
               help='Path to the ceph configuration file'),
    cfg.StrOpt('rbd_secret_uuid',
               default=None,
               help='The libvirt uuid of the secret for the rbd_user '
                    'volumes')
]

CONF = cfg.CONF
CONF.register_opt(volume_host_opt)
CONF.register_opt(volume_same_az_opt)
CONF.register_opt(az_cache_time_opt)
CONF.register_opts(rbd_opts, group="ceph")
CONF.register_opt(sbs_volume_async_host)
CONF.register_opt(sbs_volume_async_port)
CONF.register_opt(create_volume_new_customer_whitelist)
CONF.register_opt(delete_volume_new_customer_whitelist)

CONF.import_opt('glance_core_properties', 'cinder.image.glance')

LOG = logging.getLogger(__name__)
QUOTAS = quota.QUOTAS


def wrap_check_policy(func):
    """Check policy corresponding to the wrapped methods prior to execution

    This decorator requires the first 3 args of the wrapped function
    to be (self, context, volume)
    """
    @functools.wraps(func)
    def wrapped(self, context, target_obj, *args, **kwargs):
        check_policy(context, func.__name__, target_obj)
        return func(self, context, target_obj, *args, **kwargs)

    return wrapped


def check_policy(context, action, target_obj=None):
    target = {
        'project_id': context.project_id,
        'user_id': context.user_id,
    }

    if isinstance(target_obj, objects_base.CinderObject):
        # Turn object into dict so target.update can work
        target.update(objects_base.obj_to_primitive(target_obj) or {})
    else:
        target.update(target_obj or {})

    _action = 'volume:%s' % action
    cinder.policy.enforce(context, _action, target)


class API(base.Base):
    """API for interacting with the volume manager."""

    AVAILABLE_MIGRATION_STATUS = (None, 'deleting', 'error', 'success')

    def vol_init(self):
        self.vol_conf = CONF._get("ceph") 
        self.rbd_pool = self.vol_conf.rbd_pool
        self.rbd_user = self.vol_conf.rbd_user
        self.rbd_ceph_conf = self.vol_conf.rbd_ceph_conf
        self.rbd_secret_uuid = self.vol_conf.rbd_secret_uuid

        if self.rbd_pool is not None:
            self.rbd_pool = encodeutils.safe_encode(self.rbd_pool)
        
        if self.rbd_user is not None:
            self.rbd_user = encodeutils.safe_encode(self.rbd_user)
        
        if self.rbd_ceph_conf is not None:
            self.rbd_ceph_conf = encodeutils.safe_encode(self.rbd_ceph_conf)

    def __init__(self, db_driver=None, image_service=None):
        self.image_service = (image_service or
                              glance.get_default_image_service())
        self.scheduler_rpcapi = scheduler_rpcapi.SchedulerAPI()
        self.volume_rpcapi = volume_rpcapi.VolumeAPI()
        self.availability_zones = []
        self.availability_zones_last_fetched = None
        self.key_manager = keymgr.API()
        host = CONF.sbs_volume_async_host
        port = CONF.sbs_volume_async_port
        self.cinder_async_volume_client = CinderAsyncVolumeClient(host, port)
        super(API, self).__init__(db_driver)
        self.vol_init()

    def list_availability_zones(self, enable_cache=False):
        """Describe the known availability zones

        :retval tuple of dicts, each with a 'name' and 'available' key
        """
        refresh_cache = False
        if enable_cache:
            if self.availability_zones_last_fetched is None:
                refresh_cache = True
            else:
                cache_age = timeutils.delta_seconds(
                    self.availability_zones_last_fetched,
                    timeutils.utcnow())
                if cache_age >= CONF.az_cache_duration:
                    refresh_cache = True
        if refresh_cache or not enable_cache:
            topic = CONF.volume_topic
            ctxt = context.get_admin_context()
            services = self.db.service_get_all_by_topic(ctxt, topic)
            az_data = [(s['availability_zone'], s['disabled'])
                       for s in services]
            disabled_map = {}
            for (az_name, disabled) in az_data:
                tracked_disabled = disabled_map.get(az_name, True)
                disabled_map[az_name] = tracked_disabled and disabled
            azs = [{'name': name, 'available': not disabled}
                   for (name, disabled) in disabled_map.items()]
            if refresh_cache:
                now = timeutils.utcnow()
                self.availability_zones = azs
                self.availability_zones_last_fetched = now
                LOG.debug("Availability zone cache updated, next update will"
                          " occur around %s.", now + datetime.timedelta(
                              seconds=CONF.az_cache_duration))
        else:
            azs = self.availability_zones
        return tuple(azs)

    @ReportMetrics("volume-api-create")
    def create(self, context, size, name, description, snapshot=None,
               image_id=None, volume_type=None, metadata=None,
               availability_zone=None, source_volume=None,
               scheduler_hints=None,
               source_replica=None, consistencygroup=None,
               cgsnapshot=None, multiattach=False,volume_from_cache=None, backup_id=None):

        # NOTE(jdg): we can have a create without size if we're
        # doing a create from snap or volume.  Currently
        # the taskflow api will handle this and pull in the
        # size from the source.

        # NOTE(jdg): cinderclient sends in a string representation
        # of the size value.  BUT there is a possibility that somebody
        # could call the API directly so the is_int_like check
        # handles both cases (string representation of true float or int).
        if size and (not utils.is_int_like(size) or int(size) <= 0):
            msg = _('Invalid volume size provided for create request: %s '
                    '(size argument must be an integer (or string '
                    'representation of an integer) and greater '
                    'than zero).') % size
            raise exception.InvalidInput(reason=msg)

        if consistencygroup and not cgsnapshot:
            if not volume_type:
                msg = _("volume_type must be provided when creating "
                        "a volume in a consistency group.")
                raise exception.InvalidInput(reason=msg)
            cg_voltypeids = consistencygroup.get('volume_type_id')
            if volume_type.get('id') not in cg_voltypeids:
                msg = _("Invalid volume_type provided: %s (requested "
                        "type must be supported by this consistency "
                        "group).") % volume_type
                raise exception.InvalidInput(reason=msg)

        if source_volume and volume_type:
            if volume_type['id'] != source_volume['volume_type_id']:
                msg = _("Invalid volume_type provided: %s (requested type "
                        "must match source volume, "
                        "or be omitted).") % volume_type
                raise exception.InvalidInput(reason=msg)

        # When cloning replica (for testing), volume type must be omitted
        if source_replica and volume_type:
            msg = _("No volume_type should be provided when creating test "
                    "replica.")
            raise exception.InvalidInput(reason=msg)

        if snapshot and volume_type:
            if volume_type['id'] != snapshot['volume_type_id']:
                msg = _("Invalid volume_type provided: %s (requested "
                        "type must match source snapshot, or be "
                        "omitted).") % volume_type
                raise exception.InvalidInput(reason=msg)

        # Determine the valid availability zones that the volume could be
        # created in (a task in the flow will/can use this information to
        # ensure that the availability zone requested is valid).
        raw_zones = self.list_availability_zones(enable_cache=True)
        availability_zones = set([az['name'] for az in raw_zones])
        if CONF.storage_availability_zone:
            availability_zones.add(CONF.storage_availability_zone)

        create_what = {
            'context': context,
            'raw_size': size,
            'name': name,
            'description': description,
            'snapshot': snapshot,
            'image_id': image_id,
            'raw_volume_type': volume_type,
            'metadata': metadata,
            'raw_availability_zone': availability_zone,
            'source_volume': source_volume,
            'scheduler_hints': scheduler_hints,
            'key_manager': self.key_manager,
            'source_replica': source_replica,
            'optional_args': {'is_quota_committed': False},
            'consistencygroup': consistencygroup,
            'cgsnapshot': cgsnapshot,
            'multiattach': multiattach,
            'volume_from_cache':volume_from_cache,
            'backup_id':backup_id,
        }
        try:
            if cgsnapshot:
                flow_engine = create_volume.get_flow_no_rpc(self.db,
                                                            self.image_service,
                                                            availability_zones,
                                                            create_what)
            else:
                whitelisted_for_volume_async_service = self.__is_createvolume_whitelisted_for_volumeasyncservice(
                    context.project_id, image_id, snapshot)
                flow_engine = create_volume.get_flow(self.scheduler_rpcapi,
                                                     self.volume_rpcapi,
                                                     self.db,
                                                     self.image_service,
                                                     self.cinder_async_volume_client,
                                                     whitelisted_for_volume_async_service,
                                                     availability_zones,
                                                     create_what)
        except Exception:
            msg = _('Failed to create api volume flow.')
            LOG.exception(msg)
            raise exception.CinderException(msg)

        # Attaching this listener will capture all of the notifications that
        # taskflow sends out and redirect them to a more useful log for
        # cinders debugging (or error reporting) usage.
        with flow_utils.DynamicLogListener(flow_engine, logger=LOG):
            flow_engine.run()
            return flow_engine.storage.fetch('volume')

    '''
        This method determines whether to send the create calls to the new volume service.
    '''
    def __is_createvolume_whitelisted_for_volumeasyncservice(self, project_id, image_id, snapshot):
        # Dont send cache volumes or boot volumes to new volume service
        if not image_id and not snapshot:
            whitelisted_customers_text = CONF.create_volume_new_customer_whitelist
            if whitelisted_customers_text == "ALL":
                return True
            customers = whitelisted_customers_text.strip().split(',')
            for customer in customers:
                if customer == project_id:
                    return True
        return False

    @ReportMetrics("volume-api-delete")
    @wrap_check_policy
    def delete(self, context, volume, force=False, unmanage_only=False):
        if context.is_admin and context.project_id != volume['project_id']:
            project_id = volume['project_id']
        else:
            project_id = context.project_id

        volume_id = volume['id']
        if not volume['host']:
            volume_utils.notify_about_volume_usage(context,
                                                   volume, "delete.start")
            # NOTE(vish): scheduling failed, so delete it
            # Note(zhiteng): update volume quota reservation
            try:
                reserve_opts = {'volumes': -1, 'gigabytes': -volume['size']}
                QUOTAS.add_volume_type_opts(context,
                                            reserve_opts,
                                            volume['volume_type_id'])
                reservations = QUOTAS.reserve(context,
                                              project_id=project_id,
                                              **reserve_opts)
            except Exception:
                reservations = None
                LOG.exception(_LE("Failed to update quota while "
                                  "deleting volume."))
            self.db.volume_destroy(context.elevated(), volume_id)

            if reservations:
                QUOTAS.commit(context, reservations, project_id=project_id)

            volume_utils.notify_about_volume_usage(context,
                                                   volume, "delete.end")
            return
        if volume['attach_status'] == "attached":
            # Volume is still attached, need to detach first
            LOG.info(_LI('Unable to delete volume: %s, '
                         'volume is attached.'), volume['id'])
            raise exception.VolumeAttached(volume_id=volume_id)

        if not force and volume['status'] not in ["available", "error",
                                                  "error_restoring",
                                                  "error_extending"]:
            msg = _("Volume status must be available or error, "
                    "but current status is: %s.") % volume['status']
            LOG.info(_LI('Unable to delete volume: %(vol_id)s, '
                         'volume must be available or '
                         'error, but is %(vol_status)s.'),
                     {'vol_id': volume['id'],
                      'vol_status': volume['status']})
            raise exception.InvalidVolume(reason=msg)

        if volume['migration_status'] is not None:
            # Volume is migrating, wait until done
            LOG.info(_LI('Unable to delete volume: %s, '
                         'volume is currently migrating.'), volume['id'])
            msg = _("Volume cannot be deleted while migrating")
            raise exception.InvalidVolume(reason=msg)

        if volume['consistencygroup_id'] is not None:
            msg = _("Volume cannot be deleted while in a consistency group.")
            LOG.info(_LI('Unable to delete volume: %s, '
                         'volume is currently part of a '
                         'consistency group.'), volume['id'])
            raise exception.InvalidVolume(reason=msg)

        snapshots = self.db.snapshot_get_all_for_volume(context, volume_id)
        if len(snapshots):
            LOG.info(_LI('Unable to delete volume: %s, '
                         'volume currently has snapshots.'), volume['id'])
            msg = _("Volume still has %d dependent "
                    "snapshots.") % len(snapshots)
            raise exception.InvalidVolume(reason=msg)
        backups = self.db.backup_get_all_by_volume(context.elevated(), volume_id)
        if len(backups):
            for backup in backups:
                if backup['status'] == 'creating':
                    LOG.info(_LI('Unable to delete volume: %s, '
                                 'volume has snapshot %s in creating state.'), volume['id'],
                                  backup['id'])
                    msg = (_("Volume still has %s snapshot in creating state")
                            % backup['id'])

		    raise exception.InvalidVolume(reason=msg)

        # If the volume is encrypted, delete its encryption key from the key
        # manager. This operation makes volume deletion an irreversible process
        # because the volume cannot be decrypted without its key.
        encryption_key_id = volume.get('encryption_key_id', None)
        if encryption_key_id is not None:
            self.key_manager.delete_key(context, encryption_key_id)

        now = datetime.datetime.now()
        self.db.volume_update(context, volume_id, {'status': 'deleting',
                                                   'terminated_at': now})

        if self.__is_deletevolume_whitelisted_for_volumeasyncservice(volume):
            volume_context = {"requestId": context.request_id, "projectId": context.project_id}
            self.cinder_async_volume_client.deleteVolume(volume_context, volume_id)
            LOG.info(_LI("Delete volume request issued successfully to VolumeAsyncService."),
                     volume['id'])
        else:
            self.volume_rpcapi.delete_volume(context, volume, unmanage_only)
            LOG.info(_LI("Delete volume request issued successfully to cinder-volume."),
                     volume['id'])

    '''
    This method determines whether to send the create/delete calls to the new volume service.
    '''
    def __is_deletevolume_whitelisted_for_volumeasyncservice(self, volume):
        # Dont send cache volumes to new volume service
        if not volume['display_name'] or not volume['display_name'].endswith('cache_volume'):
            whitelisted_customers_text = CONF.delete_volume_new_customer_whitelist
            if whitelisted_customers_text == "ALL":
                return True
            customers = whitelisted_customers_text.strip().split(',')
            for customer in customers:
                if customer == volume['project_id']:
                    return True
        return False

    @wrap_check_policy
    def update(self, context, volume, fields):
        self.db.volume_update(context, volume['id'], fields)

    @ReportMetrics("volume-api-get")
    def get(self, context, volume_id, viewable_admin_meta=False):
        if viewable_admin_meta:
            ctxt = context.elevated()
        else:
            ctxt = context
        rv = self.db.volume_get(ctxt, volume_id)
        volume = dict(rv.iteritems())
        try:
            check_policy(context, 'get', volume)
        except exception.PolicyNotAuthorized:
            # raise VolumeNotFound instead to make sure Cinder behaves
            # as it used to
            raise exception.VolumeNotFound(volume_id=volume_id)
        return volume

    def _get_all_tenants_value(self, filters):
        """Returns a Boolean for the value of filters['all_tenants'].

           False is returned if 'all_tenants' is not in the filters dictionary.
           An InvalidInput exception is thrown for invalid values.
        """

        b = False
        if 'all_tenants' in filters:
            val = six.text_type(filters['all_tenants']).lower()
            if val in ['true', '1']:
                b = True
            elif val in ['false', '0']:
                b = False
            else:
                msg = _('all_tenants param must be 0 or 1')
                raise exception.InvalidInput(reason=msg)

        return b

    @ReportMetrics("volume-api-getall")
    def get_all(self, context, marker=None, limit=None, sort_keys=None,
                sort_dirs=None, filters=None, viewable_admin_meta=False):
        check_policy(context, 'get_all')

        if filters is None:
            filters = {}

        allTenants = self._get_all_tenants_value(filters)

        try:
            if limit is not None:
                limit = int(limit)
                if limit < 0:
                    msg = _('limit param must be positive')
                    raise exception.InvalidInput(reason=msg)
        except ValueError:
            msg = _('limit param must be an integer')
            raise exception.InvalidInput(reason=msg)

        # Non-admin shouldn't see temporary target of a volume migration, add
        # unique filter data to reflect that only volumes with a NULL
        # 'migration_status' or a 'migration_status' that does not start with
        # 'target:' should be returned (processed in db/sqlalchemy/api.py)
        if not context.is_admin:
            filters['no_migration_targets'] = True

        if filters:
            LOG.debug("Searching by: %s.", six.text_type(filters))

       
        if context.is_admin and allTenants:
            # Need to remove all_tenants to pass the filtering below.
            del filters['all_tenants']
            volumes = self.db.volume_get_all(context, marker, limit,
                                             sort_keys=sort_keys,
                                             sort_dirs=sort_dirs,
                                             filters=filters)
        else:
            if viewable_admin_meta:
                context = context.elevated()
            volumes = self.db.volume_get_all_by_project(context,
                                                        context.project_id,
                                                        marker, limit,
                                                        sort_keys=sort_keys,
                                                        sort_dirs=sort_dirs,
                                                        filters=filters)

        return volumes

    def get_snapshot(self, context, snapshot_id):
        return objects.Snapshot.get_by_id(context, snapshot_id)
    
    def get_snapshot_by_name(self,context,snapshot_name):
        return objects.Snapshot.get_by_name(context,snapshot_name)

    def get_volume_by_name(self,context,volume_name):
        rv=self.db.volume_get_by_name(context,volume_name)
        return dict(rv.iteritems())

    def get_volume(self, context, volume_id):
        check_policy(context, 'get_volume')
        rv = self.db.volume_get(context, volume_id)
        return dict(rv.iteritems())

    def _get_volume_(self, context, volume_id):
        return objects.Volume.get_by_id(context, volume_id)

    def get_all_snapshots(self, context, search_opts=None):
        check_policy(context, 'get_all_snapshots')

        search_opts = search_opts or {}

        if (context.is_admin and 'all_tenants' in search_opts):
            # Need to remove all_tenants to pass the filtering below.
            del search_opts['all_tenants']
            snapshots = self.db.snapshot_get_all(context)
        else:
            snapshots = self.db.snapshot_get_all_by_project(
                context, context.project_id)

        if search_opts:
            LOG.debug("Searching by: %s", search_opts)

            results = []
            not_found = object()
            for snapshot in snapshots:
                for opt, value in search_opts.iteritems():
                    if snapshot.get(opt, not_found) != value:
                        break
                else:
                    results.append(snapshot)
            snapshots = results
        return snapshots

    # This conditional update is enough to ensure that there are no race conditions,
    # unless we support multiattach
    @wrap_check_policy
    def reserve_volume(self, context, volume):
        #expected = {'multiattach': volume.multiattach,
        #            'status': (('available', 'in-use') if volume.multiattach
        #                       else 'available')}
        volumeObject = self._get_volume_(context, volume['id'])
        expected = { 'status': 'available' }
        result = volumeObject.conditional_update({'status': 'attaching'}, expected)

        if not result:
            msg = _("Unable to reserve volume. Volume status must be 'available' to reserve.")
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)
        LOG.info(_LI("Reserve volume completed successfully."),
                 resource=volume)

    @wrap_check_policy
    def unreserve_volume(self, context, volume):
        volumeObject = self._get_volume_(context, volume['id'])
        expected = {'status': 'attaching'}
        # Status change depends on whether it has attachments (in-use) or not
        # (available)
        value = {'status': db.Case([(db.volume_has_attachments_filter(),
                                     'in-use')],
                                   else_='available')}
        result = volumeObject.conditional_update(value, expected)
        if not result:
            msg = _("Unable to unreserve volume. Volume status must be 'attaching' to unreserve.")
            LOG.error(msg)
        LOG.info(_LI("Unreserve volume completed successfully."),
                 resource=volume)

    # This conditional update is enough to ensure that there are no race conditions,
    # unless we support multiattach
    @wrap_check_policy
    def begin_detaching(self, context, volume):
        # If we are in the middle of a volume migration, we don't want the
        # user to see that the volume is 'detaching'. Having
        # 'migration_status' set will have the same effect internally.
        volumeObject = self._get_volume_(context, volume['id'])
        expected = {'status': 'in-use',
                    'attach_status': 'attached',
                    'migration_status': self.AVAILABLE_MIGRATION_STATUS}
        result = volumeObject.conditional_update({'status': 'detaching'}, expected)

        #if not (result or self._is_volume_migrating(volume)):
        if not (result):
            msg = _("Unable to detach volume. Volume status must be 'in-use' "
                    "and attach_status must be 'attached' to detach.")
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        LOG.info(_LI("Begin detaching volume completed successfully."),
                 resource=volume)

    @wrap_check_policy
    def roll_detaching(self, context, volume):
        volumeObject = self._get_volume_(context, volume['id'])
        result = volumeObject.conditional_update({'status': 'in-use'},
                                        {'status': 'detaching'})
        if not (result):
            msg = _("Unable to roll detach volume. Volume status must be 'detaching'.")
            LOG.error(msg)
        LOG.info(_LI("Roll detaching of volume completed successfully."),
                 resource=volume)

    @ReportMetrics("volume-api-attach")
    @wrap_check_policy
    def attach(self, context, volume, instance_uuid, host_name,
               mountpoint, mode):
        volume_metadata = self.get_volume_admin_metadata(context.elevated(),
                                                         volume)
        if 'readonly' not in volume_metadata:
            # NOTE(zhiyan): set a default value for read-only flag to metadata.
            self.update_volume_admin_metadata(context.elevated(), volume,
                                              {'readonly': 'False'})
            volume_metadata['readonly'] = 'False'

        if volume_metadata['readonly'] == 'True' and mode != 'ro':
            raise exception.InvalidVolumeAttachMode(mode=mode,
                                                    volume_id=volume['id'])

        return self.attach_volume(context, volume['id'], instance_uuid,
                                  host_name, mountpoint, mode)

    @ReportMetrics("volume-api-detach")
    @wrap_check_policy
    def detach(self, context, volume, attachment_id):
        return self.detach_volume(context, volume['id'], attachment_id)

    @wrap_check_policy
    def initialize_connection(self, context, volume, connector):
        LOG.debug('initialize connection for volume-id: %(volid)s, and '
                  'connector: %(connector)s.', {'volid': volume['id'],
                                                'connector': connector})
        return self._initialize_connection(context, volume['id'], connector)

    @wrap_check_policy
    def terminate_connection(self, context, volume, connector, force=False):
        self.unreserve_volume(context, volume)
        return self._terminate_connection(context, volume['id'], connector, force)

    @wrap_check_policy
    def accept_transfer(self, context, volume, new_user, new_project):
        return self.volume_rpcapi.accept_transfer(context,
                                                  volume,
                                                  new_user,
                                                  new_project)

    def _create_snapshot(self, context,
                         volume, name, description,
                         force=False, metadata=None,
                         cgsnapshot_id=None):
        snapshot = self.create_snapshot_in_db(
            context, volume, name,
            description, force, metadata, cgsnapshot_id)
        self.volume_rpcapi.create_snapshot(context, volume, snapshot)

        return snapshot

    def create_snapshot_in_db(self, context,
                              volume, name, description,
                              force, metadata,
                              cgsnapshot_id):
        check_policy(context, 'create_snapshot', volume)

        if volume['migration_status'] is not None:
            # Volume is migrating, wait until done
            msg = _("Snapshot cannot be created while volume is migrating.")
            raise exception.InvalidVolume(reason=msg)

        if volume['status'].startswith('replica_'):
            # Can't snapshot secondary replica
            msg = _("Snapshot of secondary replica is not allowed.")
            raise exception.InvalidVolume(reason=msg)

        if ((not force) and (volume['status'] != "available")):
            msg = _("Volume %(vol_id)s status must be available, "
                    "but current status is: "
                    "%(vol_status)s.") % {'vol_id': volume['id'],
                                          'vol_status': volume['status']}
            raise exception.InvalidVolume(reason=msg)

        try:
            if CONF.no_snapshot_gb_quota:
                reserve_opts = {'snapshots': 1}
            else:
                reserve_opts = {'snapshots': 1, 'gigabytes': volume['size']}
            QUOTAS.add_volume_type_opts(context,
                                        reserve_opts,
                                        volume.get('volume_type_id'))
            reservations = QUOTAS.reserve(context, **reserve_opts)
        except exception.OverQuota as e:
            overs = e.kwargs['overs']
            usages = e.kwargs['usages']
            quotas = e.kwargs['quotas']

            def _consumed(name):
                return (usages[name]['reserved'] + usages[name]['in_use'])

            for over in overs:
                if 'gigabytes' in over:
                    msg = _LW("Quota exceeded for %(s_pid)s, tried to create "
                              "%(s_size)sG snapshot (%(d_consumed)dG of "
                              "%(d_quota)dG already consumed).")
                    LOG.warn(msg, {'s_pid': context.project_id,
                                   's_size': volume['size'],
                                   'd_consumed': _consumed(over),
                                   'd_quota': quotas[over]})
                    raise exception.VolumeSizeExceedsAvailableQuota(
                        requested=volume['size'],
                        consumed=_consumed('gigabytes'),
                        quota=quotas['gigabytes'])
                elif 'snapshots' in over:
                    msg = _LW("Quota exceeded for %(s_pid)s, tried to create "
                              "snapshot (%(d_consumed)d snapshots "
                              "already consumed).")

                    LOG.warn(msg, {'s_pid': context.project_id,
                                   'd_consumed': _consumed(over)})
                    raise exception.SnapshotLimitExceeded(
                        allowed=quotas[over])

        self._check_metadata_properties(metadata)

        snapshot = None
        try:
            kwargs = {
                'volume_id': volume['id'],
                'cgsnapshot_id': cgsnapshot_id,
                'user_id': context.user_id,
                'project_id': context.project_id,
                'status': 'creating',
                'progress': '0%',
                'volume_size': volume['size'],
                'display_name': name,
                'display_description': description,
                'volume_type_id': volume['volume_type_id'],
                'encryption_key_id': volume['encryption_key_id'],
                'metadata': metadata or {}
            }
            snapshot = objects.Snapshot(context=context, **kwargs)
            snapshot.create()

            QUOTAS.commit(context, reservations)
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    if hasattr(snapshot, 'id'):
                        snapshot.destroy()
                finally:
                    QUOTAS.rollback(context, reservations)

        return snapshot

    def create_snapshots_in_db(self, context,
                               volume_list,
                               name, description,
                               force, cgsnapshot_id):
        snapshot_list = []
        for volume in volume_list:
            self._create_snapshot_in_db_validate(context, volume, force)

        reservations = self._create_snapshots_in_db_reserve(
            context, volume_list)

        options_list = []
        for volume in volume_list:
            options = self._create_snapshot_in_db_options(
                context, volume, name, description, cgsnapshot_id)
            options_list.append(options)

        try:
            for options in options_list:
                snapshot = self.db.snapshot_create(context, options)
                snapshot_list.append(snapshot)

            QUOTAS.commit(context, reservations)
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    for snap in snapshot_list:
                        self.db.snapshot_destroy(context, snap['id'])
                finally:
                    QUOTAS.rollback(context, reservations)

        return snapshot_list

    def _create_snapshot_in_db_validate(self, context, volume, force):
        check_policy(context, 'create_snapshot', volume)

        if volume['migration_status'] is not None:
            # Volume is migrating, wait until done
            msg = _("Snapshot cannot be created while volume is migrating.")
            raise exception.InvalidVolume(reason=msg)

        if ((not force) and (volume['status'] != "available")):
            msg = _("Snapshot cannot be created because volume %(vol_id)s "
                    "is not available, current volume status: "
                    "%(vol_status)s.") % {'vol_id': volume['id'],
                                          'vol_status': volume['status']}
            raise exception.InvalidVolume(reason=msg)

    def _create_snapshots_in_db_reserve(self, context, volume_list):
        reserve_opts_list = []
        total_reserve_opts = {}
        try:
            for volume in volume_list:
                if CONF.no_snapshot_gb_quota:
                    reserve_opts = {'snapshots': 1}
                else:
                    reserve_opts = {'snapshots': 1,
                                    'gigabytes': volume['size']}
                QUOTAS.add_volume_type_opts(context,
                                            reserve_opts,
                                            volume.get('volume_type_id'))
                reserve_opts_list.append(reserve_opts)

            for reserve_opts in reserve_opts_list:
                for (key, value) in reserve_opts.items():
                    if key not in total_reserve_opts.keys():
                        total_reserve_opts[key] = value
                    else:
                        total_reserve_opts[key] = \
                            total_reserve_opts[key] + value
            reservations = QUOTAS.reserve(context, **total_reserve_opts)
        except exception.OverQuota as e:
            overs = e.kwargs['overs']
            usages = e.kwargs['usages']
            quotas = e.kwargs['quotas']

            def _consumed(name):
                return (usages[name]['reserved'] + usages[name]['in_use'])

            for over in overs:
                if 'gigabytes' in over:
                    msg = _LW("Quota exceeded for %(s_pid)s, tried to create "
                              "%(s_size)sG snapshot (%(d_consumed)dG of "
                              "%(d_quota)dG already consumed).")
                    LOG.warning(msg, {'s_pid': context.project_id,
                                      's_size': volume['size'],
                                      'd_consumed': _consumed(over),
                                      'd_quota': quotas[over]})
                    raise exception.VolumeSizeExceedsAvailableQuota(
                        requested=volume['size'],
                        consumed=_consumed('gigabytes'),
                        quota=quotas['gigabytes'])
                elif 'snapshots' in over:
                    msg = _LW("Quota exceeded for %(s_pid)s, tried to create "
                              "snapshot (%(d_consumed)d snapshots "
                              "already consumed).")

                    LOG.warning(msg, {'s_pid': context.project_id,
                                      'd_consumed': _consumed(over)})
                    raise exception.SnapshotLimitExceeded(
                        allowed=quotas[over])

        return reservations

    def _create_snapshot_in_db_options(self, context, volume,
                                       name, description,
                                       cgsnapshot_id):
        options = {'volume_id': volume['id'],
                   'cgsnapshot_id': cgsnapshot_id,
                   'user_id': context.user_id,
                   'project_id': context.project_id,
                   'status': "creating",
                   'progress': '0%',
                   'volume_size': volume['size'],
                   'display_name': name,
                   'display_description': description,
                   'volume_type_id': volume['volume_type_id'],
                   'encryption_key_id': volume['encryption_key_id']}
        return options

    def create_snapshot(self, context,
                        volume, name, description,
                        metadata=None, cgsnapshot_id=None):
        return self._create_snapshot(context, volume, name, description,
                                     False, metadata, cgsnapshot_id)

    def create_snapshot_force(self, context,
                              volume, name,
                              description, metadata=None):
        return self._create_snapshot(context, volume, name, description,
                                     True, metadata)

    @wrap_check_policy
    def delete_snapshot(self, context, snapshot, force=False):
        if not force and snapshot['status'] not in ["available", "error"]:
            LOG.error(_LE('Unable to delete snapshot: %(snap_id)s, '
                          'due to invalid status. '
                          'Status must be available or '
                          'error, not %(snap_status)s.'),
                      {'snap_id': snapshot['id'],
                       'snap_status': snapshot['status']})
            msg = _("Volume Snapshot status must be available or error.")
            raise exception.InvalidSnapshot(reason=msg)
        cgsnapshot_id = snapshot.get('cgsnapshot_id', None)
        if cgsnapshot_id:
            msg = _('Unable to delete snapshot %s because it is part of a '
                    'consistency group.') % snapshot['id']
            LOG.error(msg)
            raise exception.InvalidSnapshot(reason=msg)

        snapshot_obj = self.get_snapshot(context, snapshot['id'])
        snapshot_obj.status = 'deleting'
        snapshot_obj.save(context)

        volume = self.db.volume_get(context, snapshot_obj.volume_id)
        self.volume_rpcapi.delete_snapshot(context, snapshot_obj,
                                           volume['host'])
        LOG.info(_LI('Successfully issued request to '
                     'delete snapshot: %s'), snapshot_obj.id)

    @wrap_check_policy
    def update_snapshot(self, context, snapshot, fields):
        snapshot.update(fields)
        snapshot.save(context)

    @wrap_check_policy
    def get_volume_metadata(self, context, volume):
        """Get all metadata associated with a volume."""
        rv = self.db.volume_metadata_get(context, volume['id'])
        return dict(rv.iteritems())

    @wrap_check_policy
    def delete_volume_metadata(self, context, volume, key):
        """Delete the given metadata item from a volume."""
        self.db.volume_metadata_delete(context, volume['id'], key)

    def _check_metadata_properties(self, metadata=None):
        if not metadata:
            metadata = {}

        for k, v in metadata.iteritems():
            if len(k) == 0:
                msg = _("Metadata property key blank.")
                LOG.warn(msg)
                raise exception.InvalidVolumeMetadata(reason=msg)
            if len(k) > 255:
                msg = _("Metadata property key greater than 255 characters.")
                LOG.warn(msg)
                raise exception.InvalidVolumeMetadataSize(reason=msg)
            if len(v) > 255:
                msg = _("Metadata property value greater than 255 characters.")
                LOG.warn(msg)
                raise exception.InvalidVolumeMetadataSize(reason=msg)

    @wrap_check_policy
    def update_volume_metadata(self, context, volume, metadata, delete=False):
        """Updates or creates volume metadata.

        If delete is True, metadata items that are not specified in the
        `metadata` argument will be deleted.

        """
        if delete:
            _metadata = metadata
        else:
            orig_meta = self.get_volume_metadata(context, volume)
            _metadata = orig_meta.copy()
            _metadata.update(metadata)

        self._check_metadata_properties(_metadata)

        db_meta = self.db.volume_metadata_update(context, volume['id'],
                                                 _metadata, delete)

        # TODO(jdg): Implement an RPC call for drivers that may use this info

        return db_meta

    def get_volume_metadata_value(self, volume, key):
        """Get value of particular metadata key."""
        metadata = volume.get('volume_metadata')
        if metadata:
            for i in volume['volume_metadata']:
                if i['key'] == key:
                    return i['value']
        return None

    @wrap_check_policy
    def get_volume_admin_metadata(self, context, volume):
        """Get all administration metadata associated with a volume."""
        rv = self.db.volume_admin_metadata_get(context, volume['id'])
        return dict(rv.iteritems())

    @wrap_check_policy
    def delete_volume_admin_metadata(self, context, volume, key):
        """Delete the given administration metadata item from a volume."""
        self.db.volume_admin_metadata_delete(context, volume['id'], key)

    @wrap_check_policy
    def update_volume_admin_metadata(self, context, volume, metadata,
                                     delete=False):
        """Updates or creates volume administration metadata.

        If delete is True, metadata items that are not specified in the
        `metadata` argument will be deleted.

        """
        if delete:
            _metadata = metadata
        else:
            orig_meta = self.get_volume_admin_metadata(context, volume)
            _metadata = orig_meta.copy()
            _metadata.update(metadata)

        self._check_metadata_properties(_metadata)

        self.db.volume_admin_metadata_update(context, volume['id'],
                                             _metadata, delete)

        # TODO(jdg): Implement an RPC call for drivers that may use this info

        return _metadata

    def get_snapshot_metadata(self, context, snapshot):
        """Get all metadata associated with a snapshot."""
        snapshot_obj = self.get_snapshot(context, snapshot['id'])
        return snapshot_obj.metadata

    def delete_snapshot_metadata(self, context, snapshot, key):
        """Delete the given metadata item from a snapshot."""
        snapshot_obj = self.get_snapshot(context, snapshot['id'])
        snapshot_obj.delete_metadata_key(context, key)

    def update_snapshot_metadata(self, context,
                                 snapshot, metadata,
                                 delete=False):
        """Updates or creates snapshot metadata.

        If delete is True, metadata items that are not specified in the
        `metadata` argument will be deleted.

        """
        if delete:
            _metadata = metadata
        else:
            orig_meta = snapshot.metadata
            _metadata = orig_meta.copy()
            _metadata.update(metadata)

        self._check_metadata_properties(_metadata)

        snapshot.metadata = _metadata
        snapshot.save(context)

        # TODO(jdg): Implement an RPC call for drivers that may use this info

        return snapshot.metadata

    def get_snapshot_metadata_value(self, snapshot, key):
        pass

    def get_volumes_image_metadata(self, context):
        check_policy(context, 'get_volumes_image_metadata')
        db_data = self.db.volume_glance_metadata_get_all(context)
        results = collections.defaultdict(dict)
        for meta_entry in db_data:
            results[meta_entry['volume_id']].update({meta_entry['key']:
                                                     meta_entry['value']})
        return results

    @wrap_check_policy
    def get_volume_image_metadata(self, context, volume):
        db_data = self.db.volume_glance_metadata_get(context, volume['id'])
        return dict(
            (meta_entry.key, meta_entry.value) for meta_entry in db_data
        )

    def _check_volume_availability(self, volume, force):
        """Check if the volume can be used."""
        if volume['status'] not in ['available', 'in-use']:
            msg = _('Volume %(vol_id)s status must be '
                    'available or in-use, but current status is: '
                    '%(vol_status)s.') % {'vol_id': volume['id'],
                                          'vol_status': volume['status']}
            raise exception.InvalidVolume(reason=msg)
        if not force and 'in-use' == volume['status']:
            msg = _('Volume status is in-use.')
            raise exception.InvalidVolume(reason=msg)

    @wrap_check_policy
    def copy_volume_to_image(self, context, volume, metadata, force):
        """Create a new image from the specified volume."""
        self._check_volume_availability(volume, force)
        glance_core_properties = CONF.glance_core_properties
        if glance_core_properties:
            try:
                volume_image_metadata = self.get_volume_image_metadata(context,
                                                                       volume)
                custom_property_set = (set(volume_image_metadata).difference
                                       (set(glance_core_properties)))
                if custom_property_set:
                    metadata.update(dict(properties=dict((custom_property,
                                                          volume_image_metadata
                                                          [custom_property])
                                    for custom_property
                                    in custom_property_set)))
            except exception.GlanceMetadataNotFound:
                # If volume is not created from image, No glance metadata
                # would be available for that volume in
                # volume glance metadata table

                pass

        recv_metadata = self.image_service.create(context, metadata)
        self.update(context, volume, {'status': 'uploading'})
        self.volume_rpcapi.copy_volume_to_image(context,
                                                volume,
                                                recv_metadata)

        response = {"id": volume['id'],
                    "updated_at": volume['updated_at'],
                    "status": 'uploading',
                    "display_description": volume['display_description'],
                    "size": volume['size'],
                    "volume_type": volume['volume_type'],
                    "image_id": recv_metadata['id'],
                    "container_format": recv_metadata['container_format'],
                    "disk_format": recv_metadata['disk_format'],
                    "image_name": recv_metadata.get('name', None)}
        return response

    @wrap_check_policy
    def extend(self, context, volume, new_size):
        if volume['status'] != 'available':
            msg = _('Volume %(vol_id)s status must be available '
                    'to extend, but current status is: '
                    '%(vol_status)s.') % {'vol_id': volume['id'],
                                          'vol_status': volume['status']}
            raise exception.InvalidVolume(reason=msg)

        size_increase = (int(new_size)) - volume['size']
        if size_increase <= 0:
            msg = (_("New size for extend must be greater "
                     "than current size. (current: %(size)s, "
                     "extended: %(new_size)s).") % {'new_size': new_size,
                                                    'size': volume['size']})
            raise exception.InvalidInput(reason=msg)

        try:
            reserve_opts = {'gigabytes': size_increase}
            QUOTAS.add_volume_type_opts(context, reserve_opts,
                                        volume.get('volume_type_id'))
            reservations = QUOTAS.reserve(context, **reserve_opts)
        except exception.OverQuota as exc:
            usages = exc.kwargs['usages']
            quotas = exc.kwargs['quotas']

            def _consumed(name):
                return (usages[name]['reserved'] + usages[name]['in_use'])

            msg = _LE("Quota exceeded for %(s_pid)s, tried to extend volume "
                      "by %(s_size)sG, (%(d_consumed)dG of %(d_quota)dG "
                      "already consumed).")
            LOG.error(msg, {'s_pid': context.project_id,
                            's_size': size_increase,
                            'd_consumed': _consumed('gigabytes'),
                            'd_quota': quotas['gigabytes']})
            raise exception.VolumeSizeExceedsAvailableQuota(
                requested=size_increase,
                consumed=_consumed('gigabytes'),
                quota=quotas['gigabytes'])

        self.update(context, volume, {'status': 'extending'})
        self.volume_rpcapi.extend_volume(context, volume, new_size,
                                         reservations)

    @wrap_check_policy
    def migrate_volume(self, context, volume, host, force_host_copy):
        """Migrate the volume to the specified host."""

        # We only handle "available" volumes for now
        if volume['status'] not in ['available', 'in-use']:
            msg = _('Volume %(vol_id)s status must be available or in-use, '
                    'but current status is: '
                    '%(vol_status)s.') % {'vol_id': volume['id'],
                                          'vol_status': volume['status']}
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        # Make sure volume is not part of a migration
        if volume['migration_status'] is not None:
            msg = _("Volume %s is already part of an active "
                    "migration.") % volume['id']
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        # We only handle volumes without snapshots for now
        snaps = self.db.snapshot_get_all_for_volume(context, volume['id'])
        if snaps:
            msg = _("Volume %s must not have snapshots.") % volume['id']
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        # We only handle non-replicated volumes for now
        rep_status = volume['replication_status']
        if rep_status is not None and rep_status != 'disabled':
            msg = _("Volume %s must not be replicated.") % volume['id']
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        cg_id = volume.get('consistencygroup_id', None)
        if cg_id:
            msg = _("Volume %s must not be part of a consistency "
                    "group.") % volume['id']
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        # Make sure the host is in the list of available hosts
        elevated = context.elevated()
        topic = CONF.volume_topic
        services = self.db.service_get_all_by_topic(elevated,
                                                    topic,
                                                    disabled=False)
        found = False
        for service in services:
            svc_host = volume_utils.extract_host(host, 'backend')
            if utils.service_is_up(service) and service['host'] == svc_host:
                found = True
        if not found:
            msg = _('No available service named %s') % host
            LOG.error(msg)
            raise exception.InvalidHost(reason=msg)

        # Make sure the destination host is different than the current one
        if host == volume['host']:
            msg = _('Destination host must be different '
                    'than the current host.')
            LOG.error(msg)
            raise exception.InvalidHost(reason=msg)

        self.update(context, volume, {'migration_status': 'starting'})

        # Call the scheduler to ensure that the host exists and that it can
        # accept the volume
        volume_type = {}
        volume_type_id = volume['volume_type_id']
        if volume_type_id:
            volume_type = volume_types.get_volume_type(context, volume_type_id)
        request_spec = {'volume_properties': volume,
                        'volume_type': volume_type,
                        'volume_id': volume['id']}
        self.scheduler_rpcapi.migrate_volume_to_host(context,
                                                     CONF.volume_topic,
                                                     volume['id'],
                                                     host,
                                                     force_host_copy,
                                                     request_spec)

    @wrap_check_policy
    def migrate_volume_completion(self, context, volume, new_volume, error):
        # This is a volume swap initiated by Nova, not Cinder. Nova expects
        # us to return the new_volume_id.
        if not (volume['migration_status'] or new_volume['migration_status']):
            return new_volume['id']

        if not volume['migration_status']:
            msg = _('Source volume not mid-migration.')
            raise exception.InvalidVolume(reason=msg)

        if not new_volume['migration_status']:
            msg = _('Destination volume not mid-migration.')
            raise exception.InvalidVolume(reason=msg)

        expected_status = 'target:%s' % volume['id']
        if not new_volume['migration_status'] == expected_status:
            msg = (_('Destination has migration_status %(stat)s, expected '
                     '%(exp)s.') % {'stat': new_volume['migration_status'],
                                    'exp': expected_status})
            raise exception.InvalidVolume(reason=msg)

        return self.volume_rpcapi.migrate_volume_completion(context, volume,
                                                            new_volume, error)

    @wrap_check_policy
    def update_readonly_flag(self, context, volume, flag):
        if volume['status'] != 'available':
            msg = _('Volume %(vol_id)s status must be available '
                    'to update readonly flag, but current status is: '
                    '%(vol_status)s.') % {'vol_id': volume['id'],
                                          'vol_status': volume['status']}
            raise exception.InvalidVolume(reason=msg)
        self.update_volume_admin_metadata(context.elevated(), volume,
                                          {'readonly': six.text_type(flag)})

    @wrap_check_policy
    def retype(self, context, volume, new_type, migration_policy=None):
        """Attempt to modify the type associated with an existing volume."""
        if volume['status'] not in ['available', 'in-use']:
            msg = _('Unable to update type due to incorrect status: '
                    '%(vol_status)s on volume: %(vol_id)s. Volume status '
                    'must be available or '
                    'in-use.') % {'vol_status': volume['status'],
                                  'vol_id': volume['id']}
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        if volume['migration_status'] is not None:
            msg = (_("Volume %s is already part of an active migration.")
                   % volume['id'])
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        if migration_policy and migration_policy not in ['on-demand', 'never']:
            msg = _('migration_policy must be \'on-demand\' or \'never\', '
                    'passed: %s') % new_type
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        cg_id = volume.get('consistencygroup_id', None)
        if cg_id:
            msg = _("Volume must not be part of a consistency group.")
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        # Support specifying volume type by ID or name
        try:
            if uuidutils.is_uuid_like(new_type):
                vol_type = volume_types.get_volume_type(context, new_type)
            else:
                vol_type = volume_types.get_volume_type_by_name(context,
                                                                new_type)
        except exception.InvalidVolumeType:
            msg = _('Invalid volume_type passed: %s.') % new_type
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        vol_type_id = vol_type['id']
        vol_type_qos_id = vol_type['qos_specs_id']

        old_vol_type = None
        old_vol_type_id = volume['volume_type_id']
        old_vol_type_qos_id = None

        # Error if the original and new type are the same
        if volume['volume_type_id'] == vol_type_id:
            msg = _('New volume_type same as original: %s.') % new_type
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        if volume['volume_type_id']:
            old_vol_type = volume_types.get_volume_type(
                context, old_vol_type_id)
            old_vol_type_qos_id = old_vol_type['qos_specs_id']

        # We don't support changing encryption requirements yet
        old_enc = volume_types.get_volume_type_encryption(context,
                                                          old_vol_type_id)
        new_enc = volume_types.get_volume_type_encryption(context,
                                                          vol_type_id)
        if old_enc != new_enc:
            msg = _('Retype cannot change encryption requirements.')
            raise exception.InvalidInput(reason=msg)

        # We don't support changing QoS at the front-end yet for in-use volumes
        # TODO(avishay): Call Nova to change QoS setting (libvirt has support
        # - virDomainSetBlockIoTune() - Nova does not have support yet).
        if (volume['status'] != 'available' and
                old_vol_type_qos_id != vol_type_qos_id):
            for qos_id in [old_vol_type_qos_id, vol_type_qos_id]:
                if qos_id:
                    specs = qos_specs.get_qos_specs(context.elevated(), qos_id)
                    if specs['consumer'] != 'back-end':
                        msg = _('Retype cannot change front-end qos specs for '
                                'in-use volume: %s.') % volume['id']
                        raise exception.InvalidInput(reason=msg)

        # We're checking here in so that we can report any quota issues as
        # early as possible, but won't commit until we change the type. We
        # pass the reservations onward in case we need to roll back.
        reservations = quota_utils.get_volume_type_reservation(context, volume,
                                                               vol_type_id)

        self.update(context, volume, {'status': 'retyping'})

        request_spec = {'volume_properties': volume,
                        'volume_id': volume['id'],
                        'volume_type': vol_type,
                        'migration_policy': migration_policy,
                        'quota_reservations': reservations}

        self.scheduler_rpcapi.retype(context, CONF.volume_topic, volume['id'],
                                     request_spec=request_spec,
                                     filter_properties={})

    def manage_existing(self, context, host, ref, name=None, description=None,
                        volume_type=None, metadata=None,
                        availability_zone=None, bootable=False):
        if availability_zone is None:
            elevated = context.elevated()
            try:
                svc_host = volume_utils.extract_host(host, 'backend')
                service = self.db.service_get_by_host_and_topic(
                    elevated, svc_host, CONF.volume_topic)
            except exception.ServiceNotFound:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Unable to find service for given host.'))
            availability_zone = service.get('availability_zone')

        volume_type_id = volume_type['id'] if volume_type else None
        volume_properties = {
            'size': 0,
            'user_id': context.user_id,
            'project_id': context.project_id,
            'status': 'creating',
            'attach_status': 'detached',
            # Rename these to the internal name.
            'display_description': description,
            'display_name': name,
            'host': host,
            'availability_zone': availability_zone,
            'volume_type_id': volume_type_id,
            'metadata': metadata,
            'bootable': bootable
        }

        # Call the scheduler to ensure that the host exists and that it can
        # accept the volume
        volume = self.db.volume_create(context, volume_properties)
        request_spec = {'volume_properties': volume,
                        'volume_type': volume_type,
                        'volume_id': volume['id'],
                        'ref': ref}
        self.scheduler_rpcapi.manage_existing(context, CONF.volume_topic,
                                              volume['id'],
                                              request_spec=request_spec)
        return volume
    
    def _ceph_args(self):
        args = []
        if self.rbd_user:
            args.extend(['--id', self.rbd_user])
        if self.rbd_ceph_conf:
            args.extend(['--conf', self.rbd_ceph_conf])
        return args

    def _get_mon_addrs(self):
        args = ['ceph', 'mon', 'dump', '--format=json']
        args.extend(self._ceph_args())
        out, _ = utils.execute(*args)
        lines = out.split('\n')
        if lines[0].startswith('dumped monmap epoch'):
            lines = lines[1:]
        monmap = json.loads('\n'.join(lines))
        addrs = [mon['addr'] for mon in monmap['mons']]
        hosts = []
        ports = []
        for addr in addrs:
            host_port = addr[:addr.rindex('/')]
            host, port = host_port.rsplit(':', 1)
            hosts.append(host.strip('[]'))
            ports.append(port)
        return hosts, ports

    def _initialize_connection_(self, volume, connector):
        hosts, ports = self._get_mon_addrs()
        data = {
            'driver_volume_type': 'rbd',
            'data': {
                'name': '%s/%s' % (self.rbd_pool,
                                   volume['name']),
                'hosts': hosts,
                'ports': ports,
                'auth_enabled': (self.rbd_user is not None),
                'auth_username': self.rbd_user,
                'secret_type': 'ceph',
                'secret_uuid': self.rbd_secret_uuid, }
        }
        LOG.debug('connection data: %s', data)
        return data

    def _initialize_connection(self, context, volume_id, connector):
        """Prepare volume for connection from host represented by connector.

        This method calls the driver initialize_connection and returns
        it to the caller.  The connector parameter is a dictionary with
        information about the host that will connect to the volume in the
        following format::

            {
                'ip': ip,
                'initiator': initiator,
            }

        ip: the ip address of the connecting machine

        initiator: the iscsi initiator name of the connecting machine.
        This can be None if the connecting machine does not support iscsi
        connections.

        driver is responsible for doing any necessary security setup and
        returning a connection_info dictionary in the following format::

            {
                'driver_volume_type': driver_volume_type,
                'data': data,
            }

        driver_volume_type: a string to identify the type of volume.  This
                           can be used by the calling code to determine the
                           strategy for connecting to the volume. This could
                           be 'iscsi', 'rbd', 'sheepdog', etc.

        data: this is the data that the calling code will use to connect
              to the volume. Keep in mind that this will be serialized to
              json in various places, so it should not contain any non-json
              data types.
        """

        volume = self.db.volume_get(context, volume_id)

        # This is not used right now. Chirag
        #initiator_data = self._get_driver_initiator_data(context, connector)
        #try:
        #    if initiator_data:
        #        conn_info = self.driver.initialize_connection(volume,
        #                                                      connector,
        #                                                      initiator_data)
        #    else:
        #        conn_info = self.driver.initialize_connection(volume,
        #                                                      connector)
        try:
            conn_info = self._initialize_connection_(volume, connector)
        except Exception as err:
            err_msg = (_('Unable to fetch connection information from '
                         'backend: %(err)s') % {'err': err})
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)

        # This doesn't seem to be used. Chirag
        #initiator_update = conn_info.get('initiator_update', None)
        #if initiator_update:
        #    self._save_driver_initiator_data(context, connector,
        #                                     initiator_update)
        #    del conn_info['initiator_update']

        # Add qos_specs to connection info
        typeid = volume['volume_type_id']
        specs = None
        if typeid:
            res = volume_types.get_volume_type_qos_specs(typeid)
            qos = res['qos_specs']
            # only pass qos_specs that is designated to be consumed by
            # front-end, or both front-end and back-end.
            if qos and qos.get('consumer') in ['front-end', 'both']:
                specs = qos.get('specs')
        qos_spec = dict(qos_specs=specs)
        conn_info['data'].update(qos_spec)

        # Add access_mode to connection info
        volume_metadata = self.db.volume_admin_metadata_get(context.elevated(),
                                                            volume_id)
        if conn_info['data'].get('access_mode') is None:
            access_mode = volume_metadata.get('attached_mode')
            if access_mode is None:
                # NOTE(zhiyan): client didn't call 'os-attach' before
                access_mode = ('ro'
                               if volume_metadata.get('readonly') == 'True'
                               else 'rw')
            conn_info['data']['access_mode'] = access_mode

        return conn_info
    
    def _terminate_connection(self, context, volume_id, connector, force=False):
        """Cleanup connection from host represented by connector.

        The format of connector is the same as for initialize_connection.
        """
    
    def attach_volume(self, context, volume_id, instance_uuid, host_name,
                      mountpoint, mode):
        """Updates db to show volume is attached."""
        #Removing this as we won't have a case where multiple instances attach to a single volume. Chirag
        #@utils.synchronized(volume_id, external=True)
        def do_attach():
            # check the volume status before attaching
            volume = self.db.volume_get(context, volume_id)
            volume_metadata = self.db.volume_admin_metadata_get(
                context.elevated(), volume_id)
            if volume['status'] == 'attaching':
                if (volume_metadata.get('attached_mode') and
                    volume_metadata.get('attached_mode') != mode):
                    msg = _("being attached by different mode")
                    raise exception.InvalidVolume(reason=msg)

            if (volume['status'] == 'in-use' and not volume['multiattach']
               and not volume['migration_status']):
                msg = _("volume is already attached")
                raise exception.InvalidVolume(reason=msg)

            attachment = None
            host_name_sanitized = utils.sanitize_hostname(
                host_name) if host_name else None
            if instance_uuid:
                attachment = \
                    self.db.volume_attachment_get_by_instance_uuid(
                        context, volume_id, instance_uuid)
            else:
                attachment = \
                    self.db.volume_attachment_get_by_host(context, volume_id,
                                                          host_name_sanitized)
            if attachment is not None:
                self.db.volume_update(context, volume_id,
                                      {'status': 'in-use'})
                return

            #self._notify_about_volume_usage(context, volume, "attach.start")
            values = {'volume_id': volume_id,
                      'attach_status': 'attaching', }

            attachment = self.db.volume_attach(context.elevated(), values)
            volume_metadata = self.db.volume_admin_metadata_update(
                context.elevated(), volume_id,
                {"attached_mode": mode}, False)

            attachment_id = attachment['id']
            if instance_uuid and not uuidutils.is_uuid_like(instance_uuid):
                self.db.volume_attachment_update(context, attachment_id,
                                                 {'attach_status':
                                                  'error_attaching'})
                raise exception.InvalidUUID(uuid=instance_uuid)

            volume = self.db.volume_get(context, volume_id)

            if volume_metadata.get('readonly') == 'True' and mode != 'ro':
                self.db.volume_update(context, volume_id,
                                      {'status': 'error_attaching'})
                raise exception.InvalidVolumeAttachMode(mode=mode,
                                                        volume_id=volume_id)

            volume = self.db.volume_attached(context.elevated(),
                                             attachment_id,
                                             instance_uuid,
                                             host_name_sanitized,
                                             mountpoint,
                                             mode)
            if volume['migration_status']:
                self.db.volume_update(context, volume_id,
                                      {'migration_status': None})
            #self._notify_about_volume_usage(context, volume, "attach.end")
            return self.db.volume_attachment_get(context, attachment_id)
        return do_attach()
    
    # Removing the lock as it is required only in case of shared volumes. Chirag
    #@locked_detach_operation
    def detach_volume(self, context, volume_id, attachment_id=None):
        """Updates db to show volume is detached."""
        # TODO(vish): refactor this into a more general "unreserve"
        attachment = None
        if attachment_id:
            try:
                attachment = self.db.volume_attachment_get(context,
                                                           attachment_id)
            except exception.VolumeAttachmentNotFound:
                LOG.info(_LI("Volume detach called, but volume %(id)s not "
                             "attached."),
                         {'id': volume_id})
                # We need to make sure the volume status is set to the correct
                # status.  It could be in detaching status now, and we don't
                # want to leave it there.
                self.db.volume_detached(context, volume_id, attachment_id)
                return
        else:
            # We can try and degrade gracefuly here by trying to detach
            # a volume without the attachment_id here if the volume only has
            # one attachment.  This is for backwards compatibility.
            attachments = self.db.volume_attachment_get_used_by_volume_id(
                context, volume_id)
            if len(attachments) > 1:
                # There are more than 1 attachments for this volume
                # we have to have an attachment id.
                msg = _("Volume %(id)s is attached to more than one instance"
                        ".  A valid attachment_id must be passed to detach"
                        " this volume") % {'id': volume_id}
                LOG.error(msg)
                raise exception.InvalidVolume(reason=msg)
            elif len(attachments) == 1:
                attachment = attachments[0]
            else:
                # there aren't any attachments for this volume.
                # so set the status to available and move on.
                LOG.info(_LI("Volume detach called, but volume %(id)s not "
                             "attached."),
                         {'id': volume_id})
                self.db.volume_update(context, volume_id,
                                      {'status': 'available',
                                       'attach_status': 'detached'})
                return

        volume = self.db.volume_get(context, volume_id)
        #self._notify_about_volume_usage(context, volume, "detach.start")

        self.db.volume_detached(context.elevated(), volume_id,
                                attachment.get('id'))
        self.db.volume_admin_metadata_delete(context.elevated(), volume_id,
                                             'attached_mode')

        #volume = self.db.volume_get(context, volume_id)
        #self._notify_about_volume_usage(context, volume, "detach.end")

class HostAPI(base.Base):
    def __init__(self):
        super(HostAPI, self).__init__()

    """Sub-set of the Volume Manager API for managing host operations."""
    def set_host_enabled(self, context, host, enabled):
        """Sets the specified host's ability to accept new volumes."""
        raise NotImplementedError()

    def get_host_uptime(self, context, host):
        """Returns the result of calling "uptime" on the target host."""
        raise NotImplementedError()

    def host_power_action(self, context, host, action):
        raise NotImplementedError()

    def set_host_maintenance(self, context, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        volume evacuation.
        """
        raise NotImplementedError()
