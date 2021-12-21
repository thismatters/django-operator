import asyncio

import kopf

from services import (
    DeploymentService,
    IngressService,
    JobService,
    PodService,
    ServiceService,
)
from utils import WaitedTooLongException, superget


class DjangoKind:
    kind_services = {
        "deployment": DeploymentService,
        "service": ServiceService,
        "ingress": IngressService,
        "job": JobService,
        "pod": PodService,
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
                raise WaitedTooLongException(
                    f"Pod still running after {iterations * period} seconds"
                )
            await asyncio.sleep(period)
            return phase

    def _pod_reached_condition(self, *, namespace, name, condition):
        status = PodService().read_status(namespace=namespace, name=name)
        for _condition in status.conditions:
            if _condition.type == condition:
                return _condition.status == "True"

    async def _until_pod_ready(self, *, period=6.0, iterations=20, **pod_kwargs):
        _iterations = 0
        while not self._pod_reached_condition(condition="ready", **pod_kwargs):
            if _iterations > iterations:
                raise WaitedTooLongException(
                    f"Pod not ready after {iterations * period} seconds"
                )
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

    async def ensure_manage_commands(self, *, manage_commands, body, patch, base_kwargs):
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
        enrichments = {
            "spec": {"template": {"spec": {"initContainers": enriched_commands}}}
        }
        self._ensure(
            kind="job",
            purpose="migrations",
            enrichments=enrichments,
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

    def _migrate_deployment(
        self, *, purpose, status, enrichments, base_kwargs, skip_delete=False
    ):
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
        env_from = []
        for config_map_name in spec.get("envFromConfigMapRefs", []):
            env_from.append({"configMapRef": {"name": config_map_name}})
        for config_map_name in spec.get("envFromSecretRefs", []):
            env_from.append({"secretRef": {"name": config_map_name}})
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
                            "args": superget(spec, f"commands.{purpose}.args", []),
                            "env": spec.get("env", {}),
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
            await self._until_pod_ready(
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
        logger.info("Setting redis deployment")
        ret.update(self.ensure_redis(status=status, patch=patch, base_kwargs=_base))

        logger.info("Beginning management commands")
        # create ephemeral job for for `initManageCommands`
        manage_commands = spec.get("initManageCommands", [])
        if manage_commands:
            await self.ensure_manage_commands(
                manage_commands=manage_commands,
                patch=patch,
                body=body,
                base_kwargs=_base,
            )

        logger.info("Setting up green app deployment")
        # bring up the green app deployment
        ret.update(
            await self.ensure_green_app(
                spec=spec,
                patch=patch,
                body=body,
                status=status,
                base_kwargs=_base,
            )
        )

        logger.info("Setting up green worker deployment")
        # bring up new worker and dismiss old one
        ret.update(
            self.ensure_worker(
                spec=spec,
                status=status,
                base_kwargs=_base,
            )
        )

        logger.info("Setting up green beat deployment")
        # bring up new beat and dismiss old one
        ret.update(
            self.ensure_beat(
                spec=spec,
                status=status,
                base_kwargs=_base,
            )
        )

        logger.info("Migrating service to green app deployment")
        # update app service selector, create ingress
        ret.update(self.migrate_service(base_kwargs=_base))

        logger.info("Removing blue app deployment")
        # bring down the blue app deployment
        self.delete_blue_app(status=status, base_kwargs=_base)

        # patch status
        patch.status["condition"] = "running"
        patch.status["version"] = version
        patch.status["replicas"]["app"] = _base["app_replicas"]
        patch.status["replicas"]["worker"] = _base["worker_replicas"]
        logger.info("Migration complete. All that was green is now blue")
        kopf.info(body, reason="Ready", message="New config running")
        return ret

    def scale_deployment(
        *, namespace, body, spec, status, patch, deployment="app", **kwargs
    ):
        min_count = superget(spec, f"replicas.{deployment}", 1)
        replica_count = superget(status, f"replicas.{deployment}", 0)
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
