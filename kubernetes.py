
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
from datetime import datetime
from datetime import date
import hashlib
import socket

from twisted.internet import defer
from twisted.internet import threads
from twisted.python import log

from buildbot import config
from buildbot.interfaces import IRenderable
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.process.properties import Interpolate
from buildbot.process.properties import Properties
from buildbot.util import unicode2bytes
from buildbot.worker.docker import AbstractLatentWorker

from six import integer_types
from six import string_types
from six import text_type

from zope.interface import implementer

try:
    from kubernetes import config as kube_config
    from kubernetes import client
except ImportError as exc:
    kube_config = None
    client = None


@implementer(IRenderable)
class KubeRenderable(object):

    untouched_types = integer_types + (
        float, bool, bytes, datetime, date, type(None)
    )

    def __init__(self, kube_obj):
        self.kube_obj = kube_obj

    @defer.inlineCallbacks
    def getRenderingFor(self, props):
        res = yield self.recursive_render(
            copy.deepcopy(self.kube_obj),
            props
        )
        defer.returnValue(res)

    @defer.inlineCallbacks
    def recursive_render(self, obj, props):
        """Recursively parse kubernetes object tree to find renderable"""
        # This code is inspired by the code of kubernetes client-python
        # https://github.com/kubernetes-incubator/client-python/blob/4e593a7530a8751c817cceec715bfe1d03997793/kubernetes/client/api_client.py#L172-L214
        if isinstance(obj, string_types + (text_type,)):
            res = yield Interpolate(obj).getRenderingFor(props)
        elif isinstance(obj, tuple):
            res = yield self.recursive_render(list(obj), props)
        elif isinstance(obj, list):
            res = []
            for sub_obj in obj:
                temp = yield self.recursive_render(sub_obj, props)
                res.append(temp)
        elif isinstance(obj, dict):
            res = {}  # pylint: disable=redefined-variable-type
            for key, sub_obj in obj.item():
                res[key] = yield self.recursive_render(sub_obj, props)
        elif isinstance(obj, IRenderable):
            res = yield obj.getRenderingFor(props)
        elif isinstance(obj, self.untouched_types):
            res = obj
        else:
            for key in obj.swagger_types:
                value = getattr(obj, key)
                if not value:
                    continue
                res = yield self.recursive_render(value, props)
                setattr(obj, key, res)
            res = obj
        defer.returnValue(res)


class KubeLatentWorker(AbstractLatentWorker):
    instance = None

    properties_source = 'kube Latent Worker'

    @staticmethod
    def dependency_error():
        config.error("The python module 'kubernetes>=1' is needed to use a "
                     "KubeLatentWorker")

    def load_config(self, kubeConfig):
        try:
            kube_config.load_kube_config()
            exception = None
        except Exception:
            try:
                kube_config.load_incluster_config()
                exception = None
            except kube_config.config_exception.ConfigException as exc:
                exception = exc
        if exception and not (kubeConfig and 'host' in kubeConfig):
            config.error("No kube-apimaster host provided")
        for config_key, value in kubeConfig.items():
            setattr(client.configuration, config_key, value)


    @classmethod
    def default_job(cls):
        if not client:
            cls.dependency_error()
        job_name = '%(prop:buildername)s-%(prop:buildnumber)s'
        return client.V1Job(
            metadata=client.V1ObjectMeta(name=job_name),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(name=job_name),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name=job_name,
                                image='buildbot/buildbot-worker',
                                env=[
                                    client.V1EnvVar(
                                        name='BUILDMASTER',
                                        value='%(prop:masterFQDN)s'
                                    ),
                                    client.V1EnvVar(
                                        name='BUILDMASTER_PORT',
                                        value='%(prop:masterPort)s'
                                    ),
                                    client.V1EnvVar(
                                        name='WORKERNAME',
                                        value='%(prop:workerName)s'
                                    ),
                                    client.V1EnvVar(
                                        name='WORKERPASS',
                                        value='%(prop:workerPass)s'
                                    )
                                ]
                            )
                        ],
                        restart_policy='Never'
                    )
                )
            )
        )

    def checkConfig(self, name, password, job=None, namespace=None,
                    masterFQDN=None, getMasterMethod=None,
                    kubeConfig=None, **kwargs):
        # pylint: disable=too-many-arguments
        # pylint: disable=unused-argument

        # Set build_wait_timeout to 0 if not explicitly set: Starting a
        # container is almost immediate, we can afford doing so for each build.
        if 'build_wait_timeout' not in kwargs:
            kwargs['build_wait_timeout'] = 0
        if not client:
            self.dependency_error()
        self.load_config(kubeConfig)
        AbstractLatentWorker.checkConfig(self, name, password, **kwargs)

    @defer.inlineCallbacks
    def reconfigService(self, name, password, job=None, namespace=None,
                        masterFQDN=None, getMasterMethod=None,
                        kubeConfig=None, **kwargs):
        # pylint: disable=too-many-arguments

        # Set build_wait_timeout to 0 if not explicitly set: Starting a
        # container is almost immediate, we can afford doing so for each build.
        if 'build_wait_timeout' not in kwargs:
            kwargs['build_wait_timeout'] = 0
        if password is None:
            password = self.getRandomPass()
        # pylint: disable=attribute-defined-outside-init
        self.getMasterMethod = getMasterMethod
        if masterFQDN is None:
            # pylint: disable=invalid-name
            masterFQDN = self.get_master_qdn  # noqa: N806
        if callable(masterFQDN):
            # pylint: disable=invalid-name
            masterFQDN = masterFQDN()
        # pylint: disable=attribute-defined-outside-init
        self.masterFQDN = masterFQDN
        self.load_config(kubeConfig)
        # pylint: disable=attribute-defined-outside-init
        self.kubeConfig = kubeConfig
        # pylint: disable=attribute-defined-outside-init
        self.namespace = namespace or 'default'
        # pylint: disable=attribute-defined-outside-init
        self.job = job or KubeRenderable(self.default_job())
        # pylint: disable=invalid-name
        masterName = unicode2bytes(self.master.name)  # noqa: N806
        # pylint: disable=attribute-defined-outside-init
        self.masterhash = hashlib.sha1(masterName).hexdigest()[:6]
        yield AbstractLatentWorker.reconfigService(
            self, name, password, **kwargs)

    @defer.inlineCallbacks
    def start_instance(self, build):
        if self.instance is not None:
            raise ValueError('instance active')
        # pylint: disable=invalid-name
        masterFQDN = self.masterFQDN  # noqa: N806
        # pylint: disable=invalid-name
        masterPort = '9989'  # noqa: N806
        if self.registration is not None:
            # pylint: disable=invalid-name
            masterPort = str(self.registration.getPBPort())  # noqa: N806
        if ":" in masterFQDN:
            masterFQDN, masterPort = masterFQDN.split(':')
        master_properties = Properties.fromDict({
            'masterHash': (self.masterhash, self.properties_source),
            'masterFQDN': (masterFQDN, self.properties_source),
            'masterPort': (masterPort, self.properties_source),
            'workerName': (self.name, self.properties_source),
            'workerPass': (self.password, self.properties_source)
        })
        build.properties.updateFromProperties(master_properties)
        namespace = yield build.render(self.namespace)
        job = yield build.render(self.job)
        res = yield threads.deferToThread(
            self._thd_start_instance,
            namespace,
            job
        )
        defer.returnValue(res)

    def _thd_start_instance(self, namespace, job):
        self.load_config(self.kubeConfig)
        batch_client = client.BatchV1Api()
        # TODO: cleanup or not cleanup ?
        # cleanup the old instances

        instance = batch_client.create_namespaced_job(namespace, job)

        if instance is None:
            log.msg('Failed to create the container')
            raise LatentWorkerFailedToSubstantiate(
                'Failed to start container'
            )
        job_name = instance.metadata.name  # pylint: disable=no-member
        log.msg('Job created, Id: %s...' % job_name)
        self.instance = instance
        return [
            job_name,
            # pylint: disable=no-member
            instance.spec.template.spec.containers[0].image
        ]

    def stop_instance(self, fast=False):
        if self.instance is None:
            # be gentle. Something may just be trying to alert us that an
            # instance never attached, and it's because, somehow, we never
            # started.
            return defer.succeed(None)
        instance = self.instance
        self.instance = None
        return threads.deferToThread(self._thd_stop_instance, instance, fast)

    def _thd_stop_instance(self, instance, fast):
        # pylint: disable=unused-argument
        assert not False
        self.load_config(self.kubeConfig)
        batch_client = client.BatchV1Api()
        delete_body = client.V1DeleteOptions()
        job_name = instance.metadata.name
        namespace = instance.metadata.namespace
        log.msg('Deleting Job %s...' % job_name)
        batch_client.delete_namespaced_job(job_name, namespace, delete_body)

    def get_master_qdn(self):
        try:
            qdn_getter = self.get_master_mapping[self.getMasterMethod]
        except KeyError:
            qdn_getter = self.default_master_qdn_getter
        return qdn_getter()

    @staticmethod
    def get_fqdn():
        return socket.getfqdn()

    def get_ip(self):
        fqdn = self.get_fqdn()
        try:
            return socket.gethostbyname(fqdn)
        except socket.gaierror:
            return fqdn

    get_master_mapping = {
        'auto_ip': get_ip,
        'fqdn': get_fqdn
    }

    default_master_qdn_getter = get_ip
