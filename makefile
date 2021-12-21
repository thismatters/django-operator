build:
	@docker-compose -p djop build op
run:
	@docker-compose -p djop up -d
shell:
	@docker-compose -p djop exec op sh
prelint:
	@docker-compose -p djop exec op pip install black isort flake8
lint:
	docker-compose -p djop exec -w /op/src op /home/worker/.local/bin/black .
	docker-compose -p djop exec -w /op/src op /home/worker/.local/bin/isort .
	docker-compose -p djop exec -w /op/src op /home/worker/.local/bin/flake8 .
stop:
	@docker-compose -p djop down
