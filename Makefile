.PHONY: deploy-bot deploy-agent migrate logs shell

deploy-bot:
	cd bot && docker-compose up -d --build

deploy-agent:
	cd agent && docker-compose up -d --build

migrate:
	cd bot && docker-compose exec bot uv run alembic upgrade head

logs-bot:
	cd bot && docker-compose logs -f bot

logs-agent:
	cd agent && docker-compose logs -f agent

shell-bot:
	cd bot && docker-compose exec bot /bin/bash

shell-agent:
	cd agent && docker-compose exec agent /bin/bash

shell-db:
	cd bot && docker-compose exec db psql -U postgres -d aegis
