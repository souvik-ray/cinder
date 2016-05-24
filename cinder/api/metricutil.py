'''
Created on Jan 29, 2016

@author: souvik
'''
from metrics.ThreadLocalMetrics import ThreadLocalMetrics, ThreadLocalMetricsFactory
from metrics.Metrics import Unit
from oslo_log import log as logging
from time import time
from oslo_context.context import RequestContext as context
from metrics.metric_util import ReportMetrics as MetricUtilReportMetrics
from metrics.metric_util import MetricUtil as Metrics_MetricUtil

LOG = logging.getLogger(__name__)

'''
This decorator wraps around any method and captures latrncy around it. If the parameter 'report_error' is set to True
then it also emits metrics on whether the method throws an exception or not
'''
class ReportMetrics(MetricUtilReportMetrics):
    pass

# This creates a metrics wrapper for any method starting the method life cycle. It is recommended to put this at the
# start of a request or async flow life cycle. This cane be used as decorator
class MetricsWrapper(object):
    '''
    @program_name - This variable declares what sub component this is Cinder-API, cinder-volume etc
    @operation_name - This is API or method name for the service. For example volume-create
        '''
    def __init__(self, program_name,  operation_name):
        # Right now overriding service log path wont work
        self.__operation_name =  operation_name
        self.__program_name = program_name

    def __call__(self, function):
        def wrapped_function(*args, **kwargs):
            metricUtil = MetricUtil()
            marketplace_id = metricUtil.get_marketplace_id()
            metrics = ThreadLocalMetricsFactory(metricUtil.get_service_log_path()).with_marketplace_id(metricUtil.get_marketplace_id())\
                            .with_program_name(self.__program_name).create_metrics()
            success = 0
            fault = 0
            error = 0
            try:
                response = function(*args, **kwargs)
                success = 1
                return response
            except Exception as e:
                LOG.exception('Exception in cinderAPI: %s', e)
                fault = 1
                try:
                    if e.code < 500 and e.code > 399:
                        error = 1
                except AttributeError:
                    LOG.warn("Above Exception does not have a code")
                raise e
            finally:
                metrics.add_property("ProgramName", self.__program_name)
                metrics.add_property("OperationName", self.__operation_name)
                metrics.add_count("Success", success)
                metrics.add_count("Fault", fault)
                metrics.add_count("Error", error)
                self._add_request_attributes_to_metrics(metrics, *args, **kwargs)
                metrics.close()
        return wrapped_function

    def _add_request_attributes_to_metrics(self, metrics, *args, **kwargs):
        pass

# This class is used a as async metrics capture for example cinder volume, scheduler , backup
class CinderAsyncFlowMetricsWrapper(MetricsWrapper):
    def __init__(self,program_name,  operation_name):
        super(CinderAsyncFlowMetricsWrapper, self).__init__(program_name,
                                                          operation_name)
    def _add_request_attributes_to_metrics(self, metrics, *args, **kwargs):
        try:
            for arg in args:
                if isinstance(arg, context):
                    metrics.add_property("RequestId", arg.request_id)
                    metrics.add_property("TenantId", arg.tenant)
                    break
        except Exception as e:
            LOG.exception('Exception in Gathering metrics: %s', e)

# Wrapper for Cinder Volume
class CinderVolumeMetricsWrapper(CinderAsyncFlowMetricsWrapper):
    def __init__(self, operation_name):
        super(CinderVolumeMetricsWrapper, self).__init__("CinderVolume", operation_name)

# Wrapper for Cinder Backup
class CinderBackupMetricsWrapper(CinderAsyncFlowMetricsWrapper):
    def __init__(self,operation_name):
        super(CinderBackupMetricsWrapper, self).__init__("CinderBackup", operation_name)

# Wrapper for Cinder Scheduler
class CinderSchedulerMetricsWrapper(CinderAsyncFlowMetricsWrapper):
    def __init__(self,operation_name):
        super(CinderSchedulerMetricsWrapper, self).__init__("CinderScheduler", operation_name)

class MetricUtil(Metrics_MetricUtil):

    def get_service_log_path(self):
        # TODO: Get this from config where the rest of the logging is defined
        return "/var/log/cinder/service.log"

