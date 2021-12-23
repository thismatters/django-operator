import asyncio

import kopf

from django_operator.services import (
    DeploymentService,
    IngressService,
    JobService,
    PodService,
    ServiceService,
)
from django_operator.utils import WaitedTooLongException, merge, slugify, superget


class DjangoKind:
    kind_services = {
        "deployment": DeploymentService,
        "service": ServiceService,
        "ingress": IngressService,
        "job": JobService,
        "pod": PodService,
    }

    def __init__(self, *, logger):
        self.logger = logger

    def _ensure(self, namespace, body, kind, purpose, delete=False, **kwargs):
        kind_service_class = self.kind_services[kind]
        obj = kind_service_class(logger=self.logger).ensure(
            namespace=namespace,
            template=f"{kind}_{purpose}.yaml",
            parent=body,
            purpose=purpose,
            delete=delete,
            **kwargs,
        )
        if not delete:
            return {kind: {purpose: obj.metadata.name}}
        return {}

    def _pod_phase(self, namespace, name):
        pod = PodService(logger=self.logger).read_status(namespace=namespace, name=name)
        return pod.status.phase

    async def _until_pod_completes(self, *, period=6.0, iterations=20, **pod_kwargs):
        _iterations = 0
        _completed = ("succeeded", "failed", "unknown")
        while (phase := self._pod_phase(**pod_kwargs)) not in _completed:
            if _iterations > iterations:
                raise WaitedTooLongException(
                    f"Pod still running after {iterations * period} seconds"
                )
            _iterations += 1
            await asyncio.sleep(period)
        return phase

    def _deployment_reached_condition(self, *, namespace, name, condition):
        deployment = DeploymentService(logger=self.logger).read_status(
            namespace=namespace, name=name
        )
        self.logger.debug(f"deployment conditions: {deployment.status.conditions}")
        if deployment.status.conditions is None:
            return False
        for _condition in deployment.status.conditions:
            if _condition.type == condition:
                return _condition.status == "True"
        return False

    async def _until_deployment_available(
        self, *, period=6.0, iterations=20, **pod_kwargs
    ):
        _iterations = 0
        while not self._deployment_reached_condition(
            condition="Available", **pod_kwargs
        ):
            if _iterations > iterations:
                raise WaitedTooLongException(
                    f"Deployment not ready after {iterations * period} seconds"
                )
            _iterations += 1
            await asyncio.sleep(period)

    def ensure_redis(self, *, status, base_kwargs):
        ret = self._ensure(
            kind="deployment",
            purpose="redis",
            existing=superget(status, "created.deployment.redis"),
            **base_kwargs,
        )
        merge(
            ret,
            self._ensure(
                kind="service",
                purpose="redis",
                existing=superget(status, "created.service.redis"),
                **base_kwargs,
            ),
        )
        return ret

    async def ensure_manage_commands(
        self, *, manage_commands, spec, body, patch, base_kwargs
    ):
        enriched_commands = []
        env_from = self._get_env_from(spec=spec)
        for manage_command in manage_commands:
            _manage_command = "-".join(manage_command)
            enriched_commands.append(
                {
                    "name": slugify(_manage_command),
                    "image": base_kwargs.get("image"),
                    "command": ["python", "manage.py"] + manage_command,
                    "env": spec.get("env", []),
                    "envFrom": env_from,
                    "volumeMounts": spec.get("volumeMounts", []),
                }
            )
        enrichments = {
            "spec": {
                "imagePullSecrets": spec.get("imagePullSecrets", []),
                "volumes": spec.get("volumes", []),
                "initContainers": enriched_commands,
            }
        }
        self.logger.debug(enrichments)

        _pod = self._ensure(
            kind="pod",
            purpose="migrations",
            enrichments=enrichments,
            **base_kwargs,
        )

        # await migrations to complete
        try:
            completed_phase = await self._until_pod_completes(
                namespace=base_kwargs.get("namespace"),
                name=superget(_pod, "pod.migrations"),
            )
        except WaitedTooLongException:
            # problem
            # TODO: roll back migrations to prior version, abandon update
            patch.status["condition"] = "degraded"
            kopf.exception(body, reason="ManageCommandFailure", message="")
            raise kopf.PermanentError(
                "migrations took too long, manual intervention required!"
            )

        if completed_phase in ("failed", "unknown"):
            # TODO: roll back migrations to prior version, abandon update
            patch.status["condition"] = "degraded"
            raise kopf.PermanentError(
                "migrations failed, manual intervention required!"
            )

        patch.status["migrationVersion"] = base_kwargs.get("version")
        # TODO: collect more ganular data about last migration applied for each app.
        #  store in status

        # delete the pod
        self._ensure(
            kind="pod",
            purpose="migrations",
            existing=superget(_pod, "pod.migrations"),
            delete=True,
            **base_kwargs,
        )

    def _deployment_names(self, *, purpose, status, base_kwargs):
        existing = superget(status, f"created.deployment.{purpose}", default="")

        # see if the version changed
        if existing and existing.endswith(base_kwargs.get("version_slug")):
            former = None
        else:
            former = existing
            existing = None
        return former, existing

    def _migrate_deployment(
        self, *, purpose, status, enrichments, base_kwargs, skip_delete=False
    ):
        former_deployment, existing_deployment = self._deployment_names(
            purpose=purpose,
            status=status,
            base_kwargs=base_kwargs,
        )
        self.logger.debug(
            f"migrate {purpose} => former = {former_deployment} :: "
            f"existing = {existing_deployment} :: skip_delete = {skip_delete}"
        )

        # bring up the green deployment
        ret = self._ensure(
            kind="deployment",
            purpose=purpose,
            enrichments=enrichments,
            existing=existing_deployment,
            **base_kwargs,
        )

        current_deployment = ret["deployment"][purpose]

        # bring down the blue deployment
        if former_deployment and not skip_delete:
            self.logger.debug(f"migrate {purpose} => doing delete")
            self._ensure(
                kind="deployment",
                purpose=purpose,
                existing=former_deployment,
                delete=True,
                **base_kwargs,
            )
        return ret

    def _get_env_from(self, *, spec):
        env_from = []
        for config_map_name in spec.get("envFromConfigMapRefs", []):
            env_from.append({"configMapRef": {"name": config_map_name}})
        for config_map_name in spec.get("envFromSecretRefs", []):
            env_from.append({"secretRef": {"name": config_map_name}})
        return env_from

    def _base_enrichments(self, *, spec, purpose):
        env_from = self._get_env_from(spec=spec)
        return {
            "spec": {
                "strategy": spec.get("strategy", {}),
                "template": {
                    "spec": {
                        "imagePullSecrets": spec.get("imagePullSecrets", []),
                        "volumes": spec.get("volumes", []),
                        ("containers", 0): {
                            "command": superget(
                                spec,
                                f"commands.{purpose}.command",
                                _raise=kopf.PermanentError(
                                    f"missing {purpose} command"
                                ),
                            ),
                            "args": superget(
                                spec, f"commands.{purpose}.args", default=[]
                            ),
                            "env": spec.get("env", []),
                            "envFrom": env_from,
                            "volumeMounts": spec.get("volumeMounts", []),
                        },
                    }
                },
            }
        }

    async def ensure_green_app(self, *, patch, body, spec, status, base_kwargs):
        enrichments = self._base_enrichments(spec=spec, purpose="app")
        enrichments["spec"]["template"]["spec"][("containers", 0)].update(
            {
                "livenessProbe": spec.get("appProbeSpec", {}),
                "readinessProbe": spec.get("appProbeSpec", {}),
            }
        )
        ret = self._migrate_deployment(
            purpose="app",
            status=status,
            enrichments=enrichments,
            base_kwargs=base_kwargs,
            skip_delete=True,
        )

        # await status checks
        try:
            await self._until_deployment_available(
                namespace=base_kwargs.get("namespace"),
                name=superget(ret, "deployment.app"),
            )
        except WaitedTooLongException:
            patch.status["condition"] = "degraded"
            kopf.exception(body, reason="AppPodNotReady", message="")
            raise kopf.PermanentError("App pod not coming up :(")
        return ret

    def delete_blue_app(self, *, status, base_kwargs):
        former_deployment, _ = self._deployment_names(
            purpose="app",
            status=status,
            base_kwargs=base_kwargs,
        )

        if former_deployment:
            self.logger.debug("migrate app => doing delete")
            self._ensure(
                kind="deployment",
                purpose="app",
                existing=former_deployment,
                delete=True,
                **base_kwargs,
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
            enrichments=self._base_enrichments(spec=spec, purpose="beat"),
            base_kwargs=base_kwargs,
        )

    def migrate_service(self, *, base_kwargs):
        ret = self._ensure(
            kind="service",
            purpose="app",
            **base_kwargs,
        )

        # create Ingress
        _, common_name = base_kwargs.get("host").split(".", maxsplit=1)
        merge(
            ret,
            self._ensure(
                kind="ingress", purpose="app", common_name=common_name, **base_kwargs
            ),
        )
        return ret

    async def update_or_create(
        self, meta, spec, namespace, body, patch, status, **kwargs
    ):
        kopf.info(body, reason="Migrating", message="Enacting new config")
        patch.status["condition"] = "migrating"
        # validate by fire
        try:
            host = spec["host"]
            cluster_issuer = spec["clusterIssuer"]
            version = spec["version"]
            _image = spec["image"]
        except KeyError:
            patch.status["condition"] = "degraded"
            kopf.exception(body, reason="ConfigError", message="")
            raise kopf.PermanentError("Spec missing required field")

        self.logger.info(f"Migrating from {status.get('version', 'new')} to {version}")

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
            "version_slug": slugify(version),
            "image": image,
            "redis_port": superget(spec, "ports.redis", default=6379),
            "app_port": superget(spec, "ports.app", default=8000),
            "app_replicas": superget(
                status,
                "replicas.app",
                default=superget(spec, "replicas.app", default=1),
            ),
            "worker_replicas": superget(
                status,
                "replicas.worker",
                default=superget(spec, "replicas.worker", default=1),
            ),
        }
        self.logger.debug(f"Base kwargs: {_base}")

        # create redis deployment (this is static, so
        #   not going to worry about green-blue)
        self.logger.info("Setting redis deployment")
        merge(ret, self.ensure_redis(status=status, base_kwargs=_base))

        if status.get("migrationVersion", "zero") == version:
            self.logger.info(
                f"Already migrated to version {version}, skipping management commands"
            )
        else:
            self.logger.info("Beginning management commands")
            # create ephemeral job for for `initManageCommands`
            manage_commands = spec.get("initManageCommands", [])
            if manage_commands:
                await self.ensure_manage_commands(
                    manage_commands=manage_commands,
                    spec=spec,
                    patch=patch,
                    body=body,
                    base_kwargs=_base,
                )

        self.logger.info("Setting up green app deployment")
        # bring up the green app deployment
        merge(
            ret,
            await self.ensure_green_app(
                spec=spec,
                patch=patch,
                body=body,
                status=status,
                base_kwargs=_base,
            ),
        )

        self.logger.info("Setting up green worker deployment")
        # bring up new worker and dismiss old one
        merge(
            ret,
            self.ensure_worker(
                spec=spec,
                status=status,
                base_kwargs=_base,
            ),
        )

        self.logger.info("Setting up green beat deployment")
        # bring up new beat and dismiss old one
        merge(
            ret,
            self.ensure_beat(
                spec=spec,
                status=status,
                base_kwargs=_base,
            ),
        )

        self.logger.info("Migrating service to green app deployment")
        # update app service selector, create ingress
        merge(ret, self.migrate_service(base_kwargs=_base))

        self.logger.info("Removing blue app deployment")
        # bring down the blue app deployment
        self.delete_blue_app(status=status, base_kwargs=_base)

        # patch status
        patch.status["condition"] = "running"
        patch.status["version"] = version
        patch.status["replicas"] = {
            "app": _base["app_replicas"],
            "worker": _base["worker_replicas"],
        }
        self.logger.info("Migration complete. All that was green is now blue")
        kopf.info(body, reason="Ready", message="New config running")
        patch.status["created"] = ret
        return ret

    def scale_deployment(
        *, namespace, body, spec, status, patch, deployment="app", **kwargs
    ):
        min_count = superget(spec, f"replicas.{deployment}", default=1)
        replica_count = superget(status, f"replicas.{deployment}", default=0)
        desired_count = replica_count
        # To actually get this we need the k8s metrics server
        # https://docs.aws.amazon.com/eks/latest/userguide/metrics-server.html
        # and possibly make a PodMetrics resource; might be more trouble than
        # it's worth right now
        overall_cpu = 0.69  # IDK how to get this really
        if overall_cpu > 0.85:
            desired_count += 1
        elif overall_cpu < 0.5:
            desired_count -= 1
        desired_count = max(10, min(min_count, desired_count))

        if desired_count == replica_count:
            return
        try:
            deployment_name = superget(
                status,
                f"created.{deployment}",
                _raise=Exception(f"missing {deployment} deployment"),
            )
        except Exception:
            kopf.warn(body, reason="ScalingError", message="Cannot scale:")
            raise kopf.TemporaryError("Deployment is not ready yet", delay=45)

        deployment_name = status.get("created", {}).get(deployment)
        DeploymentService().ensure(
            namespace=namespace,
            existing=deployment_name,
            body={"spec": {"replicas": desired_count}},
        )
        patch.status["replicas"][deployment] = desired_count
