import asyncio

import kopf
import kubernetes.client.rest
import yaml

from util import merge, superget

# The useful page
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md


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
            kopf.adopt(manifest, owner=parent)
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


class WaitedTooLongException(Exception):
    pass


class DjangoKind:
    kind_services = {
        "deployment": DeploymentService,
        "service": ServiceService,
        "ingress": IngressService,
        "job": JobService,
    }

    def _ensure(self, namespace, body, kind, purpose, delete=False, **kwargs):
        kind_service_class = self.kind_services[kind]
        obj = kind_service_class().ensure(
            namespace=namespace,
            template=f"{kind}_{purpose}.yaml",
            parent=body,
            purpose=purpose,
            delete=delete,
            **kwargs,
        )
        return {kind: {purpose: obj.metadata.name}}

    def _pod_phase(self, namespace, name):
        status = PodService().read_status(namespace=namespace, name=name)
        return status.phase

    async def _until_pod_completes(self, *, period=6.0, iterations=20, **pod_kwargs):
        _iterations = 0
        _completed = ("succeeded", "failed", "unknown")
        while (phase := self._pod_phase(**pod_kwargs)) not in _completed:
            if _iterations > iterations:
                raise WaitedTooLongException
            await asyncio.sleep(period)
            return phase

    def _pod_reached_condition(self, *, namespace, name, condition):
        status = PodService().read_status(namespace=namespace, name=name)
        for _condition in status.conditions:
            if _condition.type == condition:
                return _condition.status == "True"

    async def _until_pod_ready(self, *, period=6.0, iterations=20, **pod_kwargs):
        _iterations = 0
        _ready = ("")
        while not self._pod_reached_condition(condition="ready", **pod_kwargs):
            if _iterations > iterations:
                raise WaitedTooLongException
            await asyncio.sleep(period)


    def ensure_redis(self, *, status, base_kwargs):
        ret = {}
        ret.update(
            self._ensure(
                kind="deployment",
                purpose="redis",
                existing=superget(status, "created.deployment.redis"),
                **base_kwargs,
            )
        )
        ret.update(
            self._ensure(
                kind="service",
                purpose="redis",
                existing=superget(status, "created.service.redis"),
                **base_kwargs,
            )
        )
        return ret

    def ensure_manage_commands(self, *, manage_commands, patch, base_kwargs):
        enriched_commands = []
        for manage_command in manage_commands:
            _manage_command = "-".join(manage_command)
            enriched_commands.append(
                {
                    "name": _manage_command,
                    "image": base_kwargs.get("image"),
                    "command": ["python", "manage.py"] + manage_command,
                }
            )
        migrations_enrichment = {
            "spec": {"template": {"spec": {"initContainers": enriched_commands}}}
        }
        self._ensure(
            kind="job",
            purpose="migrations",
            enrichments=migrations_enrichments,
            **base_kwargs,
        )

        # await migrations to complete
        try:
            completed_phase = await self._until_pod_completes(
                namespace=base_kwargs.get("namespace"),
                name=f"migrations-{base_kwargs.get('version')}",
            )
        except WaitedTooLongException:
            # problem
            # TODO: roll back migrations to prior version, abandon update
            patch.status["condition"] = "degraded"
            raise kopf.PermanentError(
                "migrations took too long, manual intervention required!"
            )

        if completed_phase in ("failed", "unknown"):
            # TODO: roll back migrations to prior version, abandon update
            patch.status["condition"] = "degraded"
            raise kopf.PermanentError(
                "migrations failed, manual intervention required!"
            )

        patch.status["migrationVersion"] = base_kwargs.get('version')
        # TODO: collect more ganular data about last migration applied for each app.
        #  store in status

        # delete the job pod
        self._ensure(
            kind="pod",
            purpose="migrations",
            existing=f"migrations-{base_kwargs.get('version')}",
            delete=True,
            **base_kwargs,
        )

    def _deployment_names(self, *, purpose, status, base_kwargs):
        existing_deployment = (
            superget(status, f"created.deployment.{purpose}", default=""),
        )
        # see if the version changed
        if existing_deployment.endswith(base_kwargs.get("version")):
            former_deployment = None
        else:
            former_deployment = existing_deployment
            existing_deployment = None
        return former_deployment, existing_deployment

    def _migrate_deployment(self, *, purpose, status, enrichments, base_kwargs, skip_delete=False):
        former_deployment, existing_deployment = self._deployment_names(
            purpose=purpose,
            status=status,
            base_kwargs=base_kwargs,
        )

        # bring up the green deployment
        ret = self._ensure(
            kind="deployment",
            purpose=purpose,
            enrichments=enrichments,
            existing=existing_deployment,
            **base_kwargs,
        )

        # bring down the blue deployment
        if former_deployment and not skip_delete:
            self._ensure(
                kind="deployment",
                purpose=purpose,
                existing=former_deployment,
                delete=True,
                **base_kwargs,
            )
        return ret

    def _base_enrichments(self, *, spec, purpose):
        return {
            "spec": {
                "strategy": spec.get("strategy", {}),
                "template": {
                    "spec": {
                        "imagePullSecrets": spec.get("imagePullSecrets", {}),
                        "volumes": spec.get("volumes", {}),
                        ("containers", 0): {
                            "command": superget(
                                spec,
                                f"commands.{purpose}.command",
                                _raise=kopf.PermanentError(f"missing {purpose} command"),
                            ),
                            "args": superget(spec, f"commands.{purpose}.args", []),
                            "env": spec.get("env", {}),
                            "envFrom": spec.get("envFrom", {}),
                            "volumeMounts": spec.get("volumeMounts", {}),
                        }
                    }
                }
            }
        }

    async def ensure_green_app(self, *, spec, status, base_kwargs):
        enrichments = self._base_enrichments(spec=spec, purpose="app")
        enrichments["spec"]["template"]["spec"][("containers", 0)].update({
            "livenessProbe": spec.get("appProbeSpec", {}),
            "readinessProbe": spec.get("appProbeSpec", {}),
        })
        ret = self._migrate_deployment(
            purpose="app",
            status=status,
            enrichments=enrichments,
            base_kwargs=base_kwargs,
            skip_delete=True,
        )

        # await status checks
        try:
            await self._until_pod_ready(namespace=base_kwargs.get("namespace"), name=superget(ret, "deployment.app"))
        except WaitedTooLongException:
            patch.status["condition"] = "degraded"
            raise kopf.PermanentError("App pod not coming up :(")
        return ret

    def delete_blue_app(self, *, status, base_kwargs):
        former_deployment, _ = self._deployment_names(
            purpose=purpose,
            status=status,
            base_kwargs=base_kwargs,
        )

        if former_deployment:
            self._ensure(
                kind="deployment",
                purpose="app",
                existing=former_deployment,
                delete=True,
                **_base,
            )

    def ensure_worker(self, *, spec, status, base_kwargs):
        # worker data gathering
        return self._migrate_deployment(
            purpose="worker",
            status=status,
            enrichments=self._base_enrichments(spec=spec, purpose="worker"),
            base_kwargs=base_kwargs,
        )

    def ensure_beat(self, *, spec, status, base_kwargs):
        # beat data gathering
        return self._migrate_deployment(
            purpose="beat",
            status=status,
            enrichments=self._base_enrichments(spec=spec, purpose="beat"),,
            base_kwargs=base_kwargs,
        )

    def migrate_service(self, *, base_kwargs):
        ret = {}
        ret.update(
            self._ensure(
                kind="service",
                purpose="app",
                **base_kwargs,
            )
        )

        # create Ingress
        _, common_name = base_kwargs.get("host").rsplit(".", maxsplit=1)
        ret.update(
            self._ensure(
                kind="ingress", purpose="app", common_name=common_name, **base_kwargs
            )
        )
        return ret

    async def update_or_create(
        self, meta, spec, namespace, logger, body, patch, status, **kwargs
    ):
        patch.status["condition"] = "migrating"
        # validate by fire
        try:
            host = spec["host"]
            cluster_issuer = spec["clusterIssuer"]
            version = spec["version"]
            _image = spec["image"]
        except KeyError:
            patch.status["condition"] = "degraded"
            raise kopf.PermanentError("Spec missing required field")

        logger.info(f"Migrating from {status.get('version', 'new')} to {version}")

        image = f"{_image}:{version}"
        ret = {
            "deployment": {},
            "service": {},
            "ingress": {},
        }

        # ensure namespace -- actually _don't_. this process should fail if
        #   the namespace doesn't exist

        _base = {
            "namespace": namespace,
            "body": body,
            "host": host,
            "cluster_issuer": cluster_issuer,
            "version": version,
            "image": image,
            "redis_port": superget(spec, "ports.redis", 6379),
            "app_port": superget(spec, "ports.app", 8000),
            "app_replicas": superget(
                status, "replicas.app", superget(spec, "replicas.app", 1)
            ),
            "worker_replicas": superget(
                status, "replicas.worker", superget(spec, "replicas.worker", 1)
            ),
        }


        # create redis deployment (this is static, so
        #   not going to worry about green-blue)
        logger.info(f"Setting redis deployment")
        ret.update(self.ensure_redis(status=status, patch=patch, base_kwargs=_base))

        logger.info(f"Beginning management commands")
        # create ephemeral job for for `initManageCommands`
        manage_commands = spec.get("initManageCommands", [])
        if manage_commands:
            await self.ensure_manage_commands(
                manage_commands=manage_commands,
                patch=patch,
                base_kwargs=_base,
            )

        logger.info(f"Setting up green app deployment")
        # bring up the green app deployment
        ret.update(await self.ensure_green_app(
            spec=spec,
            patch=patch,
            status=status,
            base_kwargs=_base,
        ))

        logging.info(f"Setting up green worker deployment")
        # bring up new worker and dismiss old one
        ret.update(self.ensure_worker(
            spec=spec,
            status=status,
            base_kwargs=_base,
        ))

        logging.info(f"Setting up green beat deployment")
        # bring up new beat and dismiss old one
        ret.update(self.ensure_beat(
            spec=spec,
            status=status,
            base_kwargs=_base,
        ))

        logging.info(f"Migrating service to green app deployment")
        # update app service selector, create ingress
        ret.update(self.migrate_service(base_kwargs=_base))

        logging.info(f"Removing blue app deployment")
        # bring down the blue app deployment
        self.delete_blue_app(status, base_kwargs):

        logging.info(f"Migration complete. All that was green is now blue")
        # patch status
        patch.status["condition"] = "running"
        patch.status["version"] = version
        patch.status["replicas"]["app"] = app_replicas
        patch.status["replicas"]["worker"] = worker_replicas
        return ret


@kopf.on.update("thismatters.github", "v1alpha", "django")
@kopf.on.create("thismatters.github", "v1alpha", "django")
def created(**kwargs):
    return DjangoKind().update_or_create(**kwargs)


def _scale_deployment(*, spec, status, patch, deployment="app"):
    min_count = spec.get("replicas", {}).get(deployment)
    replica_count = status.get("replicas", {}).get(deployment)
    desired_count = replica_count
    # To actually get this we need the k8s metrics server
    # https://docs.aws.amazon.com/eks/latest/userguide/metrics-server.html
    # and possibly make a PodMetrics resource; might be more trouble than
    # it's worth right now
    overall_cpu = 0.69  # IDK how to get this really
    if overal_cpu > 0.85:
        desired_count += 1
    elif overal_cpu < 0.5:
        desired_count -= 1
    desired_count = max(10, min(min_count, desired_count))

    if desired_count == replica_count:
        return
    deployment_name = status.get("created", {}).get(deployment)
    DeploymentService().ensure(
        namespace=namespace,
        existing=deployment_name,
        body={"spec": {"replicas": desired_count}},
    )
    patch.status["replicas"][deployment] = desired_count


# @kopf.on.timer("thismatters.net", "v1alpha", "django", interval=30)
# def scale_deployment(namespace, spec, status, patch, **kwargs):
#     _scale_deployment(spec=spec, status=status, patch=patch)
#     _scale_deployment(spec=spec, status=status, patch=patch, deployment="worker")
