import kopf

from django_operator.services import (
    DeploymentService,
    HorizontalPodAutoscalerService,
    IngressService,
    PodService,
    ServiceService,
)
from django_operator.utils import merge, slugify, superget


class DjangoKind:
    kind_services = {
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
            _image = spec["image"]
            version = spec["version"]
            cluster_issuer = spec["clusterIssuer"]
        except KeyError:
            patch.status["condition"] = "degraded"
            kopf.exception(body, reason="ConfigError", message="")
            raise kopf.PermanentError("Spec missing required field")

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
        self.body = body
        self.host = host
        self.spec = spec
        self.image = image
        self.patch = patch
        self.status = status
        self.version = version
        self.namespace = namespace
        self.version_slug = version_slug

    def read_resource(self, kind, purpose, name):
        kind_service_class = self.kind_services[kind]
        obj = kind_service_class(logger=self.logger).read(
            namespace=self.namespace,
            name=name,
        )
        return obj

    def delete_resource(self, *, kind, name):
        return self._ensure(kind=kind, purpose="purpose", existing=name, delete=True)

    def _ensure_raw(
        self, kind, purpose, delete=False, template=None, parent=None, **kwargs
    ):
        kind_service_class = self.kind_services[kind]
        if template is None:
            template = f"{kind}_{purpose}.yaml"
        if parent is None:
            parent = self.body
        obj = kind_service_class(logger=self.logger).ensure(
            namespace=self.namespace,
            template=template,
            purpose=purpose,
            parent=parent,
            delete=delete,
            **kwargs,
            **self.base_kwargs,
        )
        return obj

    def _ensure(self, kind, purpose, delete=False, **kwargs):
        obj = self._ensure_raw(kind, purpose, delete=delete, **kwargs)
        if not delete:
            return {kind: {purpose: obj.metadata.name}}
        return {}

    def pod_phase(self, name):
        pod = PodService(logger=self.logger).read_status(
            namespace=self.namespace, name=name
        )
        return pod.status.phase.lower()

    def deployment_reached_condition(self, *, name, condition):
        self.logger.debug(f"within deployment_reached_condition: name= {name}")
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

        _pod = self._ensure(
            kind="pod",
            purpose="migrations",
            enrichments=enrichments,
        )
        return superget(_pod, "pod.migrations")

    def clean_manage_commands(self, *, pod_name):
        # delete the pod
        self.delete_resource(kind="pod", name=pod_name)

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
        blue_name, green_name = self._resource_names(
            kind=kind,
            purpose=purpose,
        )
        self.logger.debug(
            f"migrate {purpose} {kind} => former = {blue_name} :: "
            f"existing = {green_name} :: skip_delete = {skip_delete}"
        )

        # bring up the green deployment
        green_obj = self._ensure_raw(
            kind="deployment",
            purpose=purpose,
            enrichments=enrichments,
            existing=green_name,
            template=template,
            **kwargs,
        )
        ret = {kind: {purpose: green_obj.metadata.name}}

        if kind == "deployment":
            # create horizontal pod autoscaling if appropriate
            hpa_details = superget(self.spec, f"autoscalers.{purpose}", default={})
            if hpa_details.get("enabled", False):
                hpa_kwargs = {
                    "deployment_name": green_obj.metadata.name,
                    "cpu_threshold": hpa_details["cpuUtilizationThreshold"],
                    "max_replicas": superget(hpa_details, "replicas.maximum"),
                    "min_replicas": superget(hpa_details, "replicas.minimum"),
                    "current_replicas": green_obj.spec.replicas,
                }

                if blue_name:
                    blue_obj = DeploymentService(logger=self.logger).read(
                        namespace=self.namespace,
                        name=blue_name,
                    )
                    hpa_kwargs.update({"current_replicas": blue_obj.spec.replicas})
                merge(
                    ret,
                    self._ensure(
                        kind="horizontalpodautoscaler",
                        purpose=purpose,
                        template="horizontalpodautoscaler.yaml",
                        parent=green_obj,
                        **hpa_kwargs,
                    ),
                )

        # bring down the blue_obj deployment
        if blue_name and not skip_delete:
            self.logger.debug(f"migrate {purpose} => doing delete")
            self._ensure(
                kind="deployment",
                purpose=purpose,
                existing=blue_name,
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

    def start_green(self, *, purpose):
        enrichments = self._base_enrichments(spec=self.spec, purpose=purpose)
        if purpose == "app":
            enrichments["spec"]["template"]["spec"][("containers", 0)].update(
                {
                    "livenessProbe": self.spec.get("appProbeSpec", {}),
                    "readinessProbe": self.spec.get("appProbeSpec", {}),
                }
            )
        return self._migrate_resource(
            purpose=purpose,
            enrichments=enrichments,
            skip_delete=True,
        )

    def clean_blue(self, *, purpose, blue):
        if blue:
            self.logger.debug(f"migrate {purpose} => doing delete")
            self.delete_resource(kind="deployment", name=blue)

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
