import kopf

from django_operator.services import (
    DeploymentService,
    HorizontalPodAutoscalerService,
    IngressService,
    JobService,
    PodService,
    ServiceService,
)
from django_operator.utils import merge, slugify, superget


class DjangoKind:
    kind_services = {
        "job": JobService,
        "pod": PodService,
        "ingress": IngressService,
        "service": ServiceService,
        "deployment": DeploymentService,
        "horizontalpodautoscaler": HorizontalPodAutoscalerService,
    }

    def __init__(self, *, logger, patch, body, spec, status, namespace, **_):
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
            "image": image,
            "version": version,
            "version_slug": version_slug,
            "cluster_issuer": cluster_issuer,
            "app_port": superget(spec, "ports.app"),
            "redis_port": superget(spec, "ports.redis"),
            "app_cpu_request": superget(spec, "resourceRequests.app.cpu"),
            "beat_cpu_request": superget(spec, "resourceRequests.beat.cpu"),
            "worker_cpu_request": superget(spec, "resourceRequests.worker.cpu"),
            "app_memory_request": superget(spec, "resourceRequests.app.memory"),
            "beat_memory_request": superget(spec, "resourceRequests.beat.memory"),
            "worker_memory_request": superget(spec, "resourceRequests.worker.memory"),
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

    def _ensure(self, kind, purpose, delete=False, template=None, **kwargs):
        kind_service_class = self.kind_services[kind]
        if template is None:
            template = f"{kind}_{purpose}.yaml"
        obj = kind_service_class(logger=self.logger).ensure(
            namespace=self.namespace,
            template=template,
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

    def _resource_names(self, *, kind, purpose):
        existing = superget(self.status, f"created.{kind}.{purpose}", default="")

        # see if the version changed
        if existing and existing.endswith(self.version_slug):
            former = None
        else:
            former = existing
            existing = None
        return former, existing

    def _migrate_resource(
        self,
        *,
        purpose,
        enrichments=None,
        kind="deployment",
        template=None,
        skip_delete=False,
        **kwargs,
    ):
        former_resource, existing_resource = self._resource_names(
            kind=kind,
            purpose=purpose,
        )
        self.logger.debug(
            f"migrate {purpose} {kind} => former = {former_resource} :: "
            f"existing = {existing_resource} :: skip_delete = {skip_delete}"
        )

        # bring up the green deployment
        ret = self._ensure(
            kind="deployment",
            purpose=purpose,
            enrichments=enrichments,
            existing=existing_resource,
            template=template,
            **kwargs,
        )

        # bring down the blue deployment
        if former_resource and not skip_delete:
            self.logger.debug(f"migrate {purpose} => doing delete")
            self._ensure(
                kind="deployment",
                purpose=purpose,
                existing=former_resource,
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
        return self._migrate_resource(
            purpose="app",
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
        return self._migrate_resource(
            purpose="worker",
            enrichments=self._base_enrichments(spec=self.spec, purpose="worker"),
        )

    def migrate_beat(self):
        # beat data gathering
        return self._migrate_resource(
            purpose="beat",
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

    def migrate_autoscalers(self):
        ret = {}
        for purpose, details in self.spec["autoscalers"].items():
            if not details["enabled"]:
                continue
            merge(
                ret,
                self._migrate_resource(
                    kind="horizontalpodautoscaler",
                    purpose=purpose,
                    template="horizontalpodautoscaler.yaml",
                    cpu_threshold=details["cpuUtilizationThreshold"],
                    deployment_name=superget(
                        self.status, f"created.deployment.{purpose}"
                    ),
                    max_replicas=superget(details, "replicas.maximum"),
                    min_replicas=superget(details, "replicas.minimum"),
                ),
            )
        return ret
