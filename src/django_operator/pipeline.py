import kopf


class BaseHopscotchPipelineStep:
    label_value = None
    attribute_kwargs = ("logger", "patch", "status", "retry")

    def __init__(self, **kwargs):
        # this handling of kwargs could move up a level to the pipeline itself, probably.
        self.__spec = kwargs.pop("spec")
        for attr in self.attribute_kwargs:
            setattr(self, attr, kwargs.get(attr))
        spec = self.status.get("pipelineSpec")
        if spec is None:
            spec = self.__spec
            self.patch.status["pipelineSpec"] = spec
        self.spec = spec
        self.kwargs = kwargs
        super().__init__()

    def handle(self):
        raise NotImplementedError()


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


class BaseHopscotchWaitingStep(BaseHopscotchPipelineStep)
    iterations_key = "pipelineStep.iterations"
    iterations_default = 20
    period_key = "pipelineStep.period"
    period_default = 6
    pipeline_step_noun = "pipeline step"

    def _check_timeout(self):
        max_retries = superget(self.spec, self.iterations_key, default=self.iterations_default)
        self.logger.debug(f"Retry count {self.retry}")
        if self.retry >= max_retries:
            self.patch.status["condition"] = "degraded"
            raise kopf.PermanentError(
                f"{self.pipeline_step_noun} took too long. "
                "Manual intervention required!"
            )

    def is_ready(self):
        raise NotImplementedError()

    def handle(self, *, last_output):
        if not self.is_ready(last_output=last_output):
            self._check_timeout()
            period = superget(self.spec, self.period_key, default=self.period_default)
            raise kopf.TemporaryError(
                f"The {self.pipeline_step_noun} is not complete. Waiting.", delay=period
            )
        return {}


class BeginMigrationStep(BaseHopscotchPipelineStep):
    label_value = "ready"

    def handle(self, *, last_output):
        pass


class StartManagementCommandsStep(BaseHopscotchPipelineStep, DjangoKindMixin):
    label_value = "start-mgmt"

    def handle(self, *, last_output):
        self.logger.info("Setting up redis deployment")
        created = self.django.ensure_redis()
        force_migrations = self.spec.get("alwaysRunMigrations")
        mgmt_pod = None
        migration_version = self.status.get("migrationVersion", "zero")
        if not force_migrations and migration_version == self.django.version:
            logger.info(
                f"Already migrated to version {self.django.version}, skipping "
                "management commands"
            )
        else:
            logger.info("Beginning management commands")
            mgmt_pod = self.django.start_manage_commands()
        # TODO: this should be handled by the pipeline
        # patch.metadata.labels["migration-step"] = "await-mgmt"
        return {"mgmt_pod_name": mgmt_pod, "created": created}


class AwaitManagementCommandsStep(BaseHopscotchWaitingStep, DjangoKindMixin):
    label_value = "await-mgmt"
    iterations_key = "initManageTimeouts.iterations"
    period_key = "initManageTimeouts.period"
    pipeline_step_noun = "management commands"

    def is_ready(self, *, last_output):
        mgmt_pod_name = last_output.get("mgmt_pod_name")

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


class StartGreenAppStep(BaseHopscotchPipelineStep):
    label_value = "start-app"

    def handle(self, *, last_output):
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


class AwaitGreenAppStep(BaseHopscotchWaitingStep, DjangoKindMixin):
    label_value = "await-app"

    def is_ready(self, *, last_output):
        return self.django.deployment_reached_condition(
            condition="Available", name=last_output.get("created.deployment.app"))


class MigrateServiceStep(BaseHopscotchPipelineStep):
    label_value = "migrate-service"

    def handle(self, *, last_output):
        self.logger.info("Setting up green worker deployment")
        created = self.django.migrate_worker()
        self.logger.info("Setting up green beat deployment")
        merge(created, self.django.migrate_beat())
        self.logger.info("Migrating service to green app deployment")
        merge(created, self.django.migrate_service())
        blue_app = superget(status, "complete_management_commands.blue_app")
        self.logger.info("Removing blue app deployment")
        self.django.clean_blue(purpose="app", blue=blue_app)
        logger.info("All that was green is now blue")
        kopf.info(body, reason="Ready", message="New config running")
        patch.status["version"] = django.version
        return {"created": created}


class CompleteMigrationStep(BaseHopscotchPipelineStep):
    label_value = "cleanup"

    def handle(self, *, last_output):
        created = last_output.get("created")
        patch.status["created"] = created
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


class BaseHopscotchPipeline:
    label = None
    waiting_label_value = "ready"
    steps = []

    @classmethod
    def is_step_value(cls, *, value):
        if value == cls.waiting_label_value:
            return True
        for step in cls.steps:
            if value == step.label_value:
                return True
        return False

    def handle(self, step_value, **kwargs):
        if step_value == self.waiting_label_value:
            return self.initiate_pipeline(**kwargs)
        # pull context from all prior handler run
        # run the step handler
        pass


class MigrationPipeline(BaseHopscotchPipeline):
    label = "migration-step"
    steps = [
        StartManagementCommandsStep,
        AwaitManagementCommandsStep,
        StartGreenAppStep,
        AwaitGreenAppStep,
        MigrateServiceStep,
        CompleteMigrationStep,
    ]
