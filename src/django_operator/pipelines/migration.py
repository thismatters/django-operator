import kopf
from kubernetes.client.exceptions import ApiException

from django_operator.kinds import DjangoKind
from django_operator.pipelines.base import (
    BasePipeline,
    BasePipelineStep,
    BaseWaitingStep,
)
from django_operator.utils import merge, superget


class DjangoKindMixin:
    def __init__(self):
        self.logger.debug("did DjangoKindMixin.__init__")
        self._django = None

    @property
    def django(self):
        if self._django is None:
            self._django = DjangoKind(
                spec=self.spec,
                **self.kwargs,
            )
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


class StartGreenAppStep(BasePipelineStep):
    name = "start-app"

    def handle(self, *, context):
        blue_app = superget(self.status, "created.deployment.app")
        self.logger.info("Setting up green app deployment")
        created = self.django.start_green(purpose="app")
        green_app = superget(created, "deployment.app")
        if blue_app == green_app:
            # don't bonk out the thing you just created! (just in case the
            #  version didn't change)
            blue_app = None
        # patch.metadata.labels["migration-step"] = "green-app"
        return {"blue_app": blue_app, "created": created}


class AwaitGreenAppStep(BaseWaitingStep, DjangoKindMixin):
    name = "await-app"

    def is_ready(self, *, context):
        return self.django.deployment_reached_condition(
            condition="Available", name=context.get("created.deployment.app")
        )


class MigrateServiceStep(BasePipelineStep):
    name = "migrate-service"

    def handle(self, *, context):
        self.logger.info("Setting up green worker deployment")
        created = self.django.migrate_worker()
        self.logger.info("Setting up green beat deployment")
        merge(created, self.django.migrate_beat())
        self.logger.info("Migrating service to green app deployment")
        merge(created, self.django.migrate_service())
        blue_app = superget(self.status, "complete_management_commands.blue_app")
        self.logger.info("Removing blue app deployment")
        self.django.clean_blue(purpose="app", blue=blue_app)
        self.logger.info("All that was green is now blue")
        self.patch.status["version"] = self.django.version
        return {"created": created}


class CompleteMigrationStep(BasePipelineStep):
    name = "cleanup"

    def handle(self, *, context):
        created = context.get("created")
        self.patch.status["created"] = created
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
        return {"migration_complete": complete}


class MigrationPipeline(BasePipeline):
    label = "migration-step"
    steps = [
        StartManagementCommandsStep,
        AwaitManagementCommandsStep,
        StartGreenAppStep,
        AwaitGreenAppStep,
        MigrateServiceStep,
        CompleteMigrationStep,
    ]
    update_handler_name = "migration_pipeline"

    def initiate_pipeline(self):
        kopf.info(self.body, reason="Migrating", message="Enacting new config")
        super().initiate_pipeline()
        self.patch.status["condition"] = "migrating"
        return {}

    def finalize_pipeline(self, *, context):
        if self.spec == self.__spec:
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
            self.patch.metadata.labels[self.label] = self.step_names[0]
            self.patch.status["pipelineSpec"] = self.__spec
        super().finalize_pipeline(context=context)
