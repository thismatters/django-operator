from pathlib import Path

import kopf
import kubernetes
import yaml

from utils import merge


class BaseService:
    delete_method = None
    patch_method = None
    post_method = None
    api_klass = "CoreV1Api"

    def __init__(self, *, logger):
        self.logger = logger
        self.client = getattr(kubernetes.client, self.api_klass)()

    def __transact(self, method_name, **kwargs):
        _method = getattr(self.client, method_name)
        obj = _method(**kwargs)
        return obj

    def _patch(self, **kwargs):
        return self.__transact(self.patch_method, **kwargs)

    def _post(self, **kwargs):
        return self.__transact(self.post_method, **kwargs)

    def _delete(self, **kwargs):
        return self.__transact(self.delete_method, **kwargs)

    def _render_manifest(self, *, template, **kwargs):
        _template = Path("manifests") / template
        # get template
        with open(_template) as f:
            # render template
            text = f.read().format(**kwargs)
        return yaml.safe_load(text)

    def _enrich_manifest(self, *, body, enrichments):
        if not enrichments:
            return body
        return merge(body, enrichments)

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
        obj = None
        if not delete:
            if body:
                _body = yaml.safe_load(body)
            elif template:
                _body = self._render_manifest(
                    template=template, namespace=namespace, **kwargs
                )
            else:
                raise Exception("wtf")  # config error
            _body = self._enrich_manifest(_body, enrichments)
        # post/patch template
        if existing:
            if delete:
                obj = self._delete(namespace=namespace, name=existing)
            else:
                # do patch
                obj = self._patch(namespace=namespace, name=existing, body=_body)
        elif not delete:
            kopf.adopt(_body, owner=parent)
            # do post
            obj = self._post(namespace=namespace, body=_body)
        return obj


class DeploymentService(BaseService):
    delete_method = "delete_namespaced_deployment"
    patch_method = "patch_namespaced_deployment"
    post_method = "create_namespaced_deployment"


class ServiceService(BaseService):
    delete_method = "delete_namespaced_service"
    patch_method = "patch_namespaced_service"
    post_method = "create_namespaced_service"


class IngressService(BaseService):
    delete_method = "delete_namespaced_ingress"
    patch_method = "patch_namespaced_ingress"
    post_method = "create_namespaced_ingress"


class JobService(BaseService):
    delete_method = "delete_namespaced_job"
    patch_method = "patch_namespaced_job"
    post_method = "create_namespaced_job"


class PodService(BaseService):
    """Now _this_ is what I call pod servicing!"""

    delete_method = "delete_namespaced_pod"
    read_status_method = "read_namespaced_pod_status"

    def read_status(self, **kwargs):
        return self.__transact(self.read_status_method, **kwargs)