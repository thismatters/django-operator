from pathlib import Path

import kubernetes.client
import yaml
from kubernetes.client.exceptions import ApiException, ApiValueError

from django_operator.utils import adopt_sans_labels, merge, superget

# The useful page
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md


class BaseService:
    read_method = None
    delete_method = None
    patch_method = None
    post_method = None
    read_status_method = None
    api_klass = "CoreV1Api"

    def __init__(self, *, logger):
        self.logger = logger
        self.client = getattr(kubernetes.client, self.api_klass)()

    def __transact(self, method_name, **kwargs):
        if method_name is None:
            raise NotImplementedError
        _method = getattr(self.client, method_name)
        try:
            obj = _method(**kwargs)
        except ApiException:
            self.logger.debug(f"ApiException: {kwargs.get('body', 'no body')}")
            raise
        except ApiValueError:
            self.logger.debug(f"ApiValueError: {kwargs}")
            raise
        return obj

    def _patch(self, **kwargs):
        return self.__transact(self.patch_method, **kwargs)

    def _read(self, **kwargs):
        return self.__transact(self.read_method, **kwargs)

    def _post(self, **kwargs):
        return self.__transact(self.post_method, **kwargs)

    def _delete(self, **kwargs):
        # remove the protect annotation
        try:
            return self.__transact(self.delete_method, **kwargs)
        except ApiException:
            return {}

    def unprotect(self, *, namespace, name, obj=None):
        if obj is None:
            try:
                obj = self._read(namespace=namespace, name=name)
            except ApiException:
                # object doesn't exist
                return
        _finalizers = obj.metadata.finalizers
        self.logger.debug(f"finalizers from obj {_finalizers}")
        if _finalizers is None:
            return
        finalizers = list(_finalizers)
        self.logger.debug(f"finalizers list {finalizers}")
        if "django.thismatters.github/protector" in finalizers:
            finalizers.remove("django.thismatters.github/protector")
            self.logger.debug(
                f"removed finalizer from list. these items remain {finalizers}")
        try:
            self._patch(
                body={"metadata": {"finalizers": finalizers}},
                namespace=namespace,
                name=name,
            )
        except (ApiException, ) as e:
            self.logger.error(f"removing finalizers failed for {name}")
            self.logger.error(f"{e}")
            pass

    def read_status(self, **kwargs):
        return self.__transact(self.read_status_method, **kwargs)

    def _render_manifest(self, *, template, **kwargs):
        _template = Path("manifests") / template
        # get template
        with open(_template) as f:
            # render template
            text = f.read().format(**kwargs)
        return yaml.safe_load(text)

    def _enrich_manifest(self, *, body, enrichments):
        if enrichments:
            try:
                merge(body, enrichments)
            except ValueError as e:
                self.logger.debug(f"merge failed: {e}")
                raise
        return body

    def read(self, **kwargs):
        return self._read(**kwargs)

    def ensure(
        self,
        *,
        namespace,
        template=None,
        body=None,
        parent=None,
        existing=None,
        enrichments=None,
        delete=False,
        **kwargs,
    ):
        if not delete:
            if body:
                _body = yaml.safe_load(body)
            elif template:
                _body = self._render_manifest(
                    template=template, namespace=namespace, **kwargs
                )
            else:
                raise Exception("wtf")  # config error
            _body = self._enrich_manifest(body=_body, enrichments=enrichments)
            adopt_sans_labels(_body, owner=parent, labels=("migration-step",))
            self.logger.debug(f"{_body}")
            if not existing:
                # look for an existing resource anyway
                existing = superget(_body, "metadata.name")
        _obj = None
        if existing:
            try:
                _obj = self._read(namespace=namespace, name=existing)
            except ApiException:
                existing = None
            else:
                existing = _obj.metadata.name

        # post/patch template
        obj = None
        if existing:
            if delete:
                self.unprotect(namespace=namespace, name=existing, obj=_obj)
                obj = self._delete(namespace=namespace, name=existing)
            else:
                # do patch
                obj = self._patch(namespace=namespace, name=existing, body=_body)
        elif not delete:
            # do post
            obj = self._post(namespace=namespace, body=_body)
        return obj


class DeploymentService(BaseService):
    read_method = "read_namespaced_deployment"
    delete_method = "delete_namespaced_deployment"
    patch_method = "patch_namespaced_deployment"
    post_method = "create_namespaced_deployment"
    read_status_method = "read_namespaced_deployment_status"
    api_klass = "AppsV1Api"


class ServiceService(BaseService):
    read_method = "read_namespaced_service"
    delete_method = "delete_namespaced_service"
    patch_method = "patch_namespaced_service"
    post_method = "create_namespaced_service"


class IngressService(BaseService):
    read_method = "read_namespaced_ingress"
    delete_method = "delete_namespaced_ingress"
    patch_method = "patch_namespaced_ingress"
    post_method = "create_namespaced_ingress"
    api_klass = "NetworkingV1Api"


class PodService(BaseService):
    """Now _this_ is what I call pod servicing!"""

    read_method = "read_namespaced_pod"
    delete_method = "delete_namespaced_pod"
    patch_method = "patch_namespaced_pod"
    post_method = "create_namespaced_pod"
    read_status_method = "read_namespaced_pod_status"


class HorizontalPodAutoscalerService(BaseService):
    read_method = "read_namespaced_horizontal_pod_autoscaler"
    delete_method = "delete_namespaced_horizontal_pod_autoscaler"
    patch_method = "patch_namespaced_horizontal_pod_autoscaler"
    post_method = "create_namespaced_horizontal_pod_autoscaler"
    api_klass = "AutoscalingV1Api"
