# Django Operator

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
* [] test updating (don't re-run migrations unless version changes)
* [] deploy
