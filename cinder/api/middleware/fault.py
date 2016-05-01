# Copyright 2010 United States Government as represented by the
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

from oslo_log import log as logging
import six
import webob.dec
import webob.exc

from cinder.api.openstack import wsgi
from cinder import exception
from cinder.i18n import _, _LE, _LI
from cinder import utils
from cinder import wsgi as base_wsgi
from metrics.metric_util import MetricUtil


LOG = logging.getLogger(__name__)


class FaultWrapper(base_wsgi.Middleware):
    """Calls down the middleware stack, making exceptions into faults."""

    _status_to_type = {}

    @staticmethod
    def status_to_type(status):
        if not FaultWrapper._status_to_type:
            for clazz in utils.walk_class_hierarchy(webob.exc.HTTPError):
                FaultWrapper._status_to_type[clazz.code] = clazz
        return FaultWrapper._status_to_type.get(
            status, webob.exc.HTTPInternalServerError)()

    def _error(self, inner, req):
        if not isinstance(inner, exception.QuotaError):
            LOG.error(_LE("Caught error: %s"), inner)
        safe = getattr(inner, 'safe', False)
        headers = getattr(inner, 'headers', None)
        status = getattr(inner, 'code', 500)
        if status is None:
            status = 500

        msg_dict = dict(url=req.url, status=status)
        LOG.info(_LI("%(url)s returned with HTTP %(status)d"), msg_dict)
        outer = self.status_to_type(status)
        if headers:
            outer.headers = headers
        # NOTE(johannes): We leave the explanation empty here on
        # purpose. It could possibly have sensitive information
        # that should not be returned back to the user. See
        # bugs 868360 and 874472
        # NOTE(eglynn): However, it would be over-conservative and
        # inconsistent with the EC2 API to hide every exception,
        # including those that are safe to expose, see bug 1021373
        if safe:
            msg = (inner.msg if isinstance(inner, exception.CinderException)
                   else six.text_type(inner))
            params = {'exception': inner.__class__.__name__,
                      'explanation': msg}
            outer.explanation = _('%(exception)s: %(explanation)s') % params
        return wsgi.Fault(outer)

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        response = None
        metricUtil = MetricUtil()
        metrics = metricUtil.initialize_thread_local_metrics("/var/log/cinder/service.log", "CinderAPI")
        response = None
        try:
            response = req.get_response(self.application)
        except Exception as ex:
            response = self._error(ex, req)
        finally:
            success = 0
            fault = 0
            error = 0
            try:
                status = response.status_int
                metrics.add_property("Status", status)
                if status > 399 and status < 500:
                    error = 1
                elif status > 499:
                    fault = 1
                else:
                    success = 1
            except AttributeError as e:
                LOG.exception(e)
            metrics.add_count("fault", fault)
            metrics.add_count("error", error)
            metrics.add_count("success", success)
            metrics.add_property("PathInfo", req.path_info)
            context = req.environ.get('cinder.context')
            metrics.add_property("TenantId", context.project_id)
            metrics.add_property("RemoteAddress", context.remote_address)
            metrics.add_property("RequestId", context.request_id)
            metrics.close()

        return response
