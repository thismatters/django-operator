import kopf

from django_operator.services import (
    DeploymentService,
    IngressService,
    JobService,
    PodService,
    ServiceService,
)
from django_operator.utils import merge, slugify, superget


class DjangoKind:
    kind_services = {
        "deployment": DeploymentService,
        "service": ServiceService,
        "ingress": IngressService,
        "job": JobService,
        "pod": PodService,
    }

    def __init__(self, *, logger, patch, body, spec, status, namespace):
        self.logger = logger
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
        version_slug = slugify(version)
        self.base_kwargs = {
            "host": host,
            "cluster_issuer": cluster_issuer,
            "version": version,
            "version_slug": version_slug,
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
        self.logger.debug(f"Base kwargs: {self.base_kwargs}")
        self.namespace = namespace
        self.host = host
        self.image = image
        self.version = version
        self.version_slug = version_slug
        self.patch = patch
        self.body = body
        self.spec = spec
        self.status = status

    def _ensure(self, kind, purpose, delete=False, **kwargs):
        kind_service_class = self.kind_services[kind]
        obj = kind_service_class(logger=self.logger).ensure(
            namespace=self.namespace,
            template=f"{kind}_{purpose}.yaml",
            parent=self.body,
            purpose=purpose,
            delete=delete,
            **kwargs,
            **self.base_kwargs,
        )
        if not delete:
            return {kind: {purpose: obj.metadata.name}}
        return {}

    def pod_phase(self, name):
        pod = PodService(logger=self.logger).read_status(
            namespace=self.namespace, name=name
        )
        return pod.status.phase.lower()

    def deployment_reached_condition(self, *, name, condition):
        deployment = DeploymentService(logger=self.logger).read_status(
            namespace=self.namespace, name=name
        )
        self.logger.debug(f"deployment conditions: {deployment.status.conditions}")
        if deployment.status.conditions is None:
            return False
        for _condition in deployment.status.conditions:
            if _condition.type == condition:
                return _condition.status == "True"
        return False

    def ensure_redis(self):
        ret = self._ensure(
            kind="deployment",
            purpose="redis",
            existing=superget(self.status, "created.deployment.redis"),
        )
        merge(
            ret,
            self._ensure(
                kind="service",
                purpose="redis",
                existing=superget(self.status, "created.service.redis"),
            ),
        )
        return ret

    def start_manage_commands(self):
        manage_commands = self.spec.get("initManageCommands", [])
        if manage_commands:
            return self.ensure_manage_commands(manage_commands=manage_commands)

    def ensure_manage_commands(self, *, manage_commands):
        enriched_commands = []
        env_from = self._get_env_from(spec=self.spec)
        for manage_command in manage_commands:
            _manage_command = "-".join(manage_command)
            enriched_commands.append(
                {
                    "name": slugify(_manage_command),
                    "image": self.image,
                    "command": ["python", "manage.py"] + manage_command,
                    "env": self.spec.get("env", []),
                    "envFrom": env_from,
                    "volumeMounts": self.spec.get("volumeMounts", []),
                }
            )
        enrichments = {
            "spec": {
                "imagePullSecrets": self.spec.get("imagePullSecrets", []),
                "volumes": self.spec.get("volumes", []),
                "initContainers": enriched_commands,
            }
        }
        self.logger.debug(enrichments)

        _pod = self._ensure(
            kind="pod",
            purpose="migrations",
            enrichments=enrichments,
        )
        return superget(_pod, "pod.migrations")

    def clean_manage_commands(self, *, pod_name):
        # delete the pod
        self._ensure(
            kind="pod",
            purpose="migrations",
            existing=pod_name,
            delete=True,
        )

    def _deployment_names(self, *, purpose, status):
        existing = superget(status, f"created.deployment.{purpose}", default="")

        # see if the version changed
        if existing and existing.endswith(self.version_slug):
            former = None
        else:
            former = existing
            existing = None
        return former, existing

    def _migrate_deployment(self, *, purpose, status, enrichments, skip_delete=False):
        former_deployment, existing_deployment = self._deployment_names(
            purpose=purpose,
            status=status,
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
        )

        # bring down the blue deployment
        if former_deployment and not skip_delete:
            self.logger.debug(f"migrate {purpose} => doing delete")
            self._ensure(
                kind="deployment",
                purpose=purpose,
                existing=former_deployment,
                delete=True,
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

    def start_green_app(self):
        enrichments = self._base_enrichments(spec=self.spec, purpose="app")
        enrichments["spec"]["template"]["spec"][("containers", 0)].update(
            {
                "livenessProbe": self.spec.get("appProbeSpec", {}),
                "readinessProbe": self.spec.get("appProbeSpec", {}),
            }
        )
        return self._migrate_deployment(
            purpose="app",
            status=self.status,
            enrichments=enrichments,
            skip_delete=True,
        )

    def clean_blue_app(self, blue_app):
        if blue_app:
            self.logger.debug("migrate app => doing delete")
            self._ensure(
                kind="deployment",
                purpose="app",
                existing=blue_app,
                delete=True,
            )

    def migrate_worker(self):
        # worker data gathering
        return self._migrate_deployment(
            purpose="worker",
            status=self.status,
            enrichments=self._base_enrichments(spec=self.spec, purpose="worker"),
        )

    def migrate_beat(self):
        # beat data gathering
        return self._migrate_deployment(
            purpose="beat",
            status=self.status,
            enrichments=self._base_enrichments(spec=self.spec, purpose="beat"),
        )

    def migrate_service(self):
        ret = self._ensure(
            kind="service",
            purpose="app",
        )

        # create Ingress
        _, common_name = self.host.split(".", maxsplit=1)
        merge(
            ret,
            self._ensure(kind="ingress", purpose="app", common_name=common_name),
        )
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
