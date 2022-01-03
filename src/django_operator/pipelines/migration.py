import kopf
from kubernetes.client.exceptions import ApiException

from django_operator.kinds import DjangoKind
from django_operator.pipelines.base import (
    BasePipeline,
    BasePipelineStep,
    BaseWaitingStep,
)
from django_operator.utils import superget


class DjangoKindMixin:
    def __init__(self):
        self._django = None

    @property
    def django(self):
        if self._django is None:
            self._django = DjangoKind(**self.kwargs)
        return self._django


class StartManagementCommandsStep(BasePipelineStep, DjangoKindMixin):
    name = "start-mgmt"

    def handle(self, *, context):
        self.logger.info("Setting up redis deployment")
        created = self.django.ensure_redis()
        force_migrations = self.spec.get("alwaysRunMigrations")
        mgmt_pod = None
        migration_version = self.status.get("migrationVersion", "zero")
        if not force_migrations and migration_version == self.django.version:
            self.logger.info(
                f"Already migrated to version {self.django.version}, skipping "
                "management commands"
            )
        else:
            self.logger.info("Beginning management commands")
            mgmt_pod = self.django.start_manage_commands()
        return {"mgmt_pod_name": mgmt_pod, "created": created}


class AwaitManagementCommandsStep(BaseWaitingStep, DjangoKindMixin):
    name = "await-mgmt"
    iterations_key = "initManageTimeouts.iterations"
    period_key = "initManageTimeouts.period"
    pipeline_step_noun = "management commands"

    def is_ready(self, *, context):
        mgmt_pod_name = context.get("mgmt_pod_name")

        if mgmt_pod_name:
            try:
                pod_phase = self.django.pod_phase(mgmt_pod_name)
            except ApiException:
                pod_phase = "unknown"
            if pod_phase in ("failed", "unknown"):
                self.patch.status["condition"] = "degraded"
                raise kopf.PermanentError(
                    f"{self.pipeline_step_noun} have failed. "
                    "Manual intervention required!"
                )
            if pod_phase != "succeeded":
                return False
            self.django.clean_manage_commands(pod_name=mgmt_pod_name)
            self.patch.status["migrationVersion"] = self.django.version
        return True


class StartGreenDeploymentStep(BasePipelineStep, DjangoKindMixin):
    def handle(self, *, context):
        blue = superget(self.status, f"created.deployment.{self.purpose}")
        self.logger.info(f"Setting up green {self.purpose} deployment")
        created = self.django.start_green(purpose=self.purpose)
        green = superget(created, f"deployment.{self.purpose}")
        if blue == green:
            # don't bonk out the thing you just created! (just in case the
            #  version didn't change)
            blue = None
        return {f"blue_{self.purpose}": blue, "created": created}


class AwaitGreenDeploymentStep(BaseWaitingStep, DjangoKindMixin):
    def is_ready(self, *, context):
        return self.django.deployment_reached_condition(
            condition="Available",
            name=superget(context, f"created.deployment.{self.purpose}"),
        )


class StartGreenAppStep(StartGreenDeploymentStep):
    name = "start-app"
    purpose = "app"


class AwaitGreenAppStep(AwaitGreenDeploymentStep):
    name = "await-app"
    purpose = "app"


class StartGreenWorkerStep(StartGreenDeploymentStep):
    name = "start-worker"
    purpose = "worker"


class AwaitGreenWorkerStep(AwaitGreenDeploymentStep):
    name = "await-worker"
    purpose = "worker"


class StartGreenBeatStep(StartGreenDeploymentStep):
    name = "start-beat"
    purpose = "beat"


class AwaitGreenBeatStep(AwaitGreenDeploymentStep):
    name = "await-beat"
    purpose = "beat"
    period_default = 3


class MigrateServiceStep(BasePipelineStep, DjangoKindMixin):
    name = "migrate-service"

    def handle(self, *, context):
        self.logger.info("Migrating service to green app deployment")
        created = self.django.migrate_service()
        self.patch.status["version"] = self.django.version
        return {"created": created}


class CompleteMigrationStep(BasePipelineStep, DjangoKindMixin):
    name = "cleanup"

    def handle(self, *, context):
        created = context.get("created")
        create_targets = [
            "deployment.app",
            "deployment.beat",
            "deployment.redis",
            "deployment.worker",
            "ingress.app",
            "service.app",
            "service.redis",
        ]
        for purpose in ("app", "worker"):
            if superget(self.spec, f"autoscalers.{purpose}.enabled", default=False):
                create_targets.append(f"horizontalpodautoscaler.{purpose}")
        complete = all([superget(created, t) is not None for t in create_targets])

        if complete:
            self.patch.status["created"] = created
            # remove the blue resources
            for purpose in ("beat", "worker", "app"):
                self.logger.info(f"Removing blue {purpose} deployment")
                self.django.clean_blue(
                    purpose="app", blue=superget(context, f"blue_{purpose}")
                )
            self.logger.info("All that was green is now blue")
        else:
            # remove any created green resources (that aren't part of the blue deployment)
            self.logger.info("Migration was incomplete, rolling back to prior state")
            for purpose in ("beat", "worker", "app"):
                _green = superget(created, f"deployment.{purpose}")
                _blue = superget(self.status, f"created.deployment.{purpose}")
                if _green != _blue:
                    self.django.delete_resource(kind="deployment", name=_green)
            self.django.delete_resource(
                kind="pod", name=superget(context, "mgmt_pod_name")
            )
        return {"migration_complete": complete}


class MonitorException(Exception):
    pass


class MigrationPipeline(BasePipeline, DjangoKindMixin):
    label = "migration-step"
    steps = [
        StartManagementCommandsStep,
        AwaitManagementCommandsStep,
        StartGreenAppStep,
        AwaitGreenAppStep,
        StartGreenWorkerStep,
        AwaitGreenWorkerStep,
        StartGreenBeatStep,
        AwaitGreenBeatStep,
        MigrateServiceStep,
        CompleteMigrationStep,
    ]
    update_handler_name = "migration_pipeline"

    def initiate_pipeline(self):
        super().initiate_pipeline()
        self.patch.status["condition"] = "migrating"
        return {}

    def finalize_pipeline(self, *, context):
        if self.spec == self._spec:
            if context.get("migration_complete", False):
                self.patch.status["condition"] = "running"
                self.logger.info("Migration complete.")
            else:
                self.patch.status["condition"] = "degraded"
                self.logger.info("Something went wrong; manual intervention required")
            kopf.info(self.body, reason="Ready", message="New config running")
            self.patch.metadata.labels[self.label] = self.waiting_step_name
            self.patch.status["pipelineSpec"] = None
        else:
            self.logger.info("Object changed during migration. Starting new migration.")
            self.patch.metadata.labels[self.label] = self.steps[0].name
            self.patch.status["pipelineSpec"] = self._spec
        super().finalize_pipeline(context=context)

    def monitor(self):
        problem = False
        for kind, data in self.status.get("created").items():
            for purpose, name in data.items():
                try:
                    obj = self.django.read_resource(
                        kind=kind, purpose=purpose, name=name
                    )
                except ApiException:
                    self.logger.error(f"{purpose} {kind} {name} missing.")
                    problem = True
                else:
                    # check for deleted tag
                    if getattr(obj.metadata, "deletion_timestamp", False):
                        self.logger.error(
                            f"{purpose} {kind} {name} marked for deletion."
                        )
                        problem = True
        if problem:
            # start the pipeline
            kopf.warn(self.body, reason="Migrating", message="Something is missing...")
            self.initiate_pipeline()
            raise MonitorException()

    def unprotect_all(self):
        for kind, data in self.status.get("created").items():
            for purpose, name in data.items():
                self.django.unprotect_resource(kind=kind, name=name)
