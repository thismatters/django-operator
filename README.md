# Django Operator

**This is severely alpha software, so use it at your own risk. There is no warranty, etc. I am looking for someone willing to review/vet the code. If you're interested please reach out by creating an issue!**

A [Kubernetes operator](https://github.com/cncf/tag-app-delivery/blob/main/operator-wg/whitepaper/Operator-WhitePaper_v1-0.md) for a Django stack including Celery, django-celery-beat, and redis.

Provides a `Django` CRD which:
* Coordinates deployment of app
  * creates ingresses for app
  * creates certs for app
  * ensures one beat (`django-celery-beat`)
  * scales app and worker containers independently
* Coordinates initialization of app proper
  * handles migrations
  * handles fixtures
  * handles initialization management commands

Takes some inspiration from [21h/django-operator](https://git.blindage.org/21h/django-operator) which is presented as a freestanding operator written in `go`.

Prior to use you will need to install the django operator onto your cluster with:
```
kubectl apply -f https://raw.githubusercontent.com/thismatters/django-operator/main/django-operator.yml
```

See [sample.yaml](sample.yaml) for a sample manifest for a django app.

## TODO:

* [x] Update secrets in staging, prod once tested
* [x] Figure out global pattern (https://github.com/nolar/kopf/issues/876) -- monolithic!
* [x] write CRD
* [x] create manifests for:
  * [x] deployment -- app
  * [x] deployment -- worker
  * [x] deployment -- beat
  * [x] deployment -- redis
  * [x] ingress -- app
  * [x] service -- redis
  * [x] service -- app
  * [x] job -- migrations
* [x] write create/update the code
* [x] more logging, send events
* [] write delete code (shouldn't be much here really...)
* [x] set up CI pipeline
* [x] lint
* [x] deploy to cluster (for testing)
* [x] test (create a new namespace for testing, and use an arbitrary URL)
* [x] test updating (don't re-run migrations unless version changes )
* [x] deploy

### v0.1.0
* [x] better logging
* [x] de-monolith the process
* [x] labels ("migration-step": "green-app") are contaminating the ingresses
* [x] incorporate metrics from metric server into autoscaling. (use horizontal pod autoscaling instead!)
* [x] unittests! -- could probably still use more tho
* [x] Explore a more declarative pattern
* [ ] documentation for users
* [ ] more active monitoring of child resources (prevent deletion, &c.)
* [ ] Better tracking of objects as they are created for garbage collection in event of failed migration, more robust cleanup should include garbage collection
* [ ] More paths to the cleanup step
* [ ] Consider restructuring the CRD to have sections for each purpose (`{"app": {"xyz": "val", "abc": "val"}}` as opposed to `{"xyz": {"app": "val", "worker": "val"}}`)

### v0.2.0
* [ ] allow other manifests (deployments, ingresses, services) to be set in django manifest
* [ ] manage a database; to facilitate smoke-test deployments -- allow deployment to be defined in the django manifest
