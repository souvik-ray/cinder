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
# Copyright (c) 2016 Reliance JIO Corporation
# Copyright (c) 2016 Shishir Gowda <shishir.gowda@ril.com>

from oslo_log import log as logging
from sqlalchemy import Column, MetaData, Table, Float, Integer, BigInteger

from cinder.i18n import _LE

LOG = logging.getLogger(__name__)


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    backups = Table('backups', meta, autoload=True)

    try:
        backups.c.actual_size.alter(BigInteger())
    except Exception:
        LOG.error(_LE("Changing actual_size to BigInt failed for  backups."))
        raise


def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    backups = Table('backups', meta, autoload=True)

    try:
        backups.c.actual_size.alter(Integer())
    except Exception:
        LOG.error(_LE("Changing actual_size from BigInt to Int from backups table failed."))
        raise
