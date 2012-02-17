import getpass
import os
import pprint
import subprocess
import time

from twitter.common import log
from twitter.mesos import clusters
from twitter.mesos.location import Location

from twitter.mesos.scheduler_client import SchedulerClient
from twitter.mesos.session_key_helper import SessionKeyHelper
from twitter.mesos.tunnel_helper import TunnelHelper
from twitter.mesos.updater import Updater

from gen.twitter.mesos.ttypes import *

class MesosHelper(object):
  _DEFAULT_USER = getpass.getuser()

  @staticmethod
  def acquire_session_key_or_die(owner):
    key = SessionKey(user=owner)
    SessionKeyHelper.sign_session(key, owner)
    return key

  @staticmethod
  def call(cmd, host, user=_DEFAULT_USER):
    if host is not None:
      return subprocess.call(['ssh', '%s@%s' % (user, host), ' '.join(cmd)])
    else:
      return subprocess.call(cmd)

  @staticmethod
  def check_call(cmd, host, user=_DEFAULT_USER):
    if host is not None:
      return subprocess.check_call(['ssh', '%s@%s' % (user, host), ' '.join(cmd)])
    else:
      return subprocess.check_call(cmd)

  @staticmethod
  def assert_valid_cluster(cluster):
    assert cluster, "Cluster not specified!"
    if cluster.find(':') > -1:
      scluster = cluster.split(':')

      if scluster[0] != 'localhost':
        clusters.assert_exists(scluster[0])

      if len(scluster) == 2:
        try:
          int(scluster[1])
        except ValueError as e:
          log.fatal('The cluster argument is invalid: %s (error: %s)' % (cluster, e))
          assert False, 'Invalid cluster argument: %s' % cluster
    else:
      clusters.assert_exists(cluster)

  @staticmethod
  def copy_to_hadoop(user, ssh_proxy_host, src, dst):
    """
    Copy the local file src to a hadoop path dst. Should be used in
    conjunction with starting new mesos jobs
    """
    abs_src = os.path.expanduser(src)
    dst_dir = os.path.dirname(dst)

    assert os.path.exists(abs_src), 'App file does not exist, cannot continue - %s' % abs_src

    hdfs_src = abs_src if Location.is_prod() else os.path.basename(abs_src)

    if ssh_proxy_host:
      log.info('Running in corp, copy will be done via %s@%s' % (user, ssh_proxy_host))
      subprocess.check_call(['scp', abs_src, '%s@%s:' % (user, ssh_proxy_host)])

    if not MesosHelper.call(['hadoop', 'fs', '-test', '-e', dst], ssh_proxy_host, user=user):
      log.info("Deleting existing file at %s" % dst)
      MesosHelper.check_call(['hadoop', 'fs', '-rm', dst], ssh_proxy_host, user=user)
    elif MesosHelper.call(['hadoop', 'fs', '-test', '-e', dst_dir], ssh_proxy_host, user=user):
      log.info('Creating directory %s' % dst_dir)
      MesosHelper.check_call(['hadoop', 'fs', '-mkdir', dst_dir], ssh_proxy_host, user=user)

    log.info('Copying %s -> %s' % (hdfs_src, dst))
    MesosHelper.check_call(['hadoop', 'fs', '-put', hdfs_src, dst], ssh_proxy_host, user=user)

  @staticmethod
  def copy_app_to_hadoop(user, source_path, hdfs_path, cluster, ssh_proxy):
    assert hdfs_path is not None, 'No target HDFS path specified'
    hdfs = clusters.get_hadoop_uri(cluster)
    hdfs_uri = '%s%s' % (hdfs, hdfs_path)
    MesosHelper.copy_to_hadoop(user, ssh_proxy, source_path, hdfs_uri)

class MesosClientBase(object):
  """
  This class is responsible for creating a thrift client
  to the twitter scheduler. Basically all the dirty work
  needed by the MesosClientAPI.
  """

  def __init__(self, cluster, verbose=False):
    self._cluster = cluster
    self._session_key = self._client = self._proxy = self._scheduler = None
    self.verbose = verbose
    MesosHelper.assert_valid_cluster(cluster)

  def with_scheduler(method):
    """Decorator magic to make sure a connection is made to the scheduler"""
    def _wrapper(self, *args, **kwargs):
      if not self._scheduler:
        self._construct_scheduler()
      return method(self, *args, **kwargs)
    return _wrapper

  @with_scheduler
  def client(self):
    return self._client

  @with_scheduler
  def proxy(self):
    return self._proxy

  @with_scheduler
  def cluster(self):
    return self._cluster

  def _construct_scheduler(self):
    """
      Populates:
        self._proxy (if proxy necessary)
        self._scheduler
        self._client
    """
    self._proxy, self._scheduler = SchedulerClient.get(self._cluster, verbose=self.verbose)
    assert self._scheduler, "Could not find scheduler (cluster = %s)" % self._cluster
    self._client = self._scheduler.get_thrift_client()
    assert self._client, "Could not construct thrift client."

  def acquire_session(self):
    return MesosHelper.acquire_session_key_or_die(getpass.getuser())


class MesosClientAPI(MesosClientBase):
  """This class provides the API to talk to the twitter scheduler"""

  def __init__(self, **kwargs):
    super(MesosClientAPI, self).__init__(**kwargs)

  def requires_auth(method):
    def _wrapper(self, *args, **kwargs):
      if not self._session_key:
        self._session_key = self.acquire_session()
      return method(self, *args, **kwargs)
    return _wrapper

  @requires_auth
  def create_job(self, config, copy_app_from=None):
    if copy_app_from is not None:
      MesosHelper.copy_app_to_hadoop(config.role(), copy_app_from,
          config.hdfs_path(), self.cluster(), self.proxy())

    log.info('Creating job %s' % config.name())
    return self.client().createJob(config.job(), self._session_key)

  @requires_auth
  def start_cronjob(self, role, jobname):
    log.info("Starting cron job: %s" % jobname)

    return self.client().startCronJob(role, jobname, self._session_key)

  @requires_auth
  def kill_job(self, role, jobname):
    log.info("Killing tasks for job: %s" % jobname)

    query = TaskQuery()
    query.owner = Identity(role=role)
    query.jobName = jobname

    return self.client().killTasks(query, self._session_key)

  def check_status(self, role, jobname):
    log.info("Checking status of job: %s" % jobname)

    query = TaskQuery()
    query.owner = Identity(role=role)
    query.jobName = jobname

    return self.client().getTasksStatus(query)

  @requires_auth
  def update_job(self, config, copy_app_from=None):
    log.info("Updating job: %s" % config.name())

    if copy_app_from is not None:
      MesosHelper.copy_app_to_hadoop(config.role(), copy_app_from,
          config().hdfs_path(), self.cluster(), self.proxy())

    resp = self.client().startUpdate(config.job(), self._session_key)

    if resp.responseCode != ResponseCode.OK:
      log.info("Error doing start update: %s" % resp.message)

      # Create a update response and return it
      update_resp = FinishUpdateResponse()
      update_resp.responseCode = ResponseCode.INVALID_REQUEST
      update_resp.message = resp.message
      return update_resp

    # TODO(William Farner): Cleanly handle connection failures in case the scheduler
    #                       restarts mid-update.
    updater = Updater(config.role(), config.name(), self.client(), time, resp.updateToken,
                      self._session_key)
    failed_shards = updater.update(config.job())

    if failed_shards:
      log.info('Update reverted, failures detected on shards %s' % failed_shards)
    else:
      log.info('Update Successful')

    resp = self.client().finishUpdate(
      config.role(), config.name(), UpdateResult.FAILED if failed_shards else UpdateResult.SUCCESS,
      resp.updateToken, self._session_key)

    if resp.responseCode != ResponseCode.OK:
      log.info("Error doing finish update: %s" % resp.message)

      # Create a update response and return it
      update_resp = FinishUpdateResponse()
      update_resp.responseCode = ResponseCode.INVALID_REQUEST
      update_resp.message = resp.message
      return update_resp

    resp = FinishUpdateResponse()
    resp.responseCode = ResponseCode.WARNING if failed_shards else ResponseCode.OK
    resp.message = "Update Unsuccessful" if failed_shards else "Update Successful"
    return resp

  @requires_auth
  def cancel_update(self, jobname):
    log.info("Canceling update on job: %s" % jobname)

    resp = self.client().finishUpdate(role, jobname, UpdateResult.TERMINATE,
        None, self._session_key)

    if resp.responseCode != ResponseCode.OK:
      log.info("Error cancelling the update: %s" % resp.message)

      # Create a update response and return it
      update_resp = FinishUpdateResponse()
      update_resp.responseCode = ResponseCode.INVALID_REQUEST
      update_resp.message = resp.message
      return update_resp

    resp = FinishUpdateResponse()
    resp.responseCode = ResponseCode.OK
    resp.message = "Update Cancelled"
    return resp

  def get_quota(self, role):
    log.info("Getting quota for: %s" % role)

    return self.client().getQuota(role)

  @requires_auth
  def set_quota(self, role, cpu, ram_mb, disk_mb):
    log.info("Setting quota for user:%s cpu:%f ram_mb:%d disk_mb: %d"
              % (role, cpu, ram_mb, disk_mb))

    return self.client().setQuota(role, Quota(cpu, ram_mb, disk_mb), self._session_key)

  @requires_auth
  def force_task_state(self, task_id, status):
    log.info("Requesting that task %s transition to state %s" % (task_id, status))
    return self.client().forceTaskState(task_id, status, self._session_key)

