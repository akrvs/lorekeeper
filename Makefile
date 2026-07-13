.PHONY: up down logs ps psql rebuild clean mcp ingest sync-github sync-slack demo demo-down

DEMO_NET := company-brain-demo-net
DEMO_DB  := company-brain-demo-db

up:            ## Build + start the stack (DB then API)
	docker compose up --build -d

down:          ## Stop the stack (keeps data volume)
	docker compose down

logs:          ## Tail API logs
	docker compose logs -f api

ps:            ## Show service status
	docker compose ps

psql:          ## Open a psql shell against the running DB
	docker compose exec db psql -U $${POSTGRES_USER:-brain} -d $${POSTGRES_DB:-company_brain}

ingest:        ## Trigger ingestion via the API, e.g. `make ingest SOURCE=github`
	curl -s -X POST localhost:$${API_PORT:-8000}/ingest/$${SOURCE:-github} | python3 -m json.tool

sync-github:   ## Live GitHub sync via CLI, e.g. `make sync-github REPO=owner/name`
	docker compose run --rm -T api python -m app.pipeline --source github --repo $(REPO)

sync-slack:    ## Live Slack sync via CLI, e.g. `make sync-slack CHANNEL=C0123456789`
	docker compose run --rm -T api python -m app.pipeline --source slack --channel $(CHANNEL)

mcp:           ## Launch the MCP server over stdio (what an MCP client runs)
	docker compose run --rm -T mcp

rebuild:       ## Force a clean image rebuild
	docker compose build --no-cache

clean:         ## Stop the stack AND delete the data volume (destructive)
	docker compose down -v

demo:          ## 1-click offline demo: ingest demo_vault/ and print example MCP queries
	@docker network create $(DEMO_NET) >/dev/null 2>&1 || true
	@docker rm -f $(DEMO_DB) >/dev/null 2>&1 || true
	@docker run -d --name $(DEMO_DB) --network $(DEMO_NET) -p 5432:5432 \
		-e POSTGRES_USER=brain -e POSTGRES_PASSWORD=brain -e POSTGRES_DB=company_brain \
		pgvector/pgvector:pg16 >/dev/null
	@printf "Starting demo database"; \
		until docker exec $(DEMO_DB) pg_isready -U brain -d company_brain >/dev/null 2>&1; \
		do printf "."; sleep 1; done; echo " ready"
	@echo "Building image..."; docker build -q -t company-brain:latest . >/dev/null 2>&1
	@docker run --rm --network $(DEMO_NET) \
		-e POSTGRES_HOST=$(DEMO_DB) -e POSTGRES_USER=brain -e POSTGRES_PASSWORD=brain \
		-e POSTGRES_DB=company_brain -e LLM_PROVIDER=stub -e LOCAL_ROOT=/vault \
		-v "$(CURDIR)/demo_vault":/vault:ro \
		company-brain:latest python -m app.demo
	@echo ""
	@echo "Demo DB '$(DEMO_DB)' is still running (localhost:5432) so you can point an"
	@echo "MCP client at it. Tear it down with: make demo-down"

demo-down:     ## Stop and remove the demo database
	@docker rm -f $(DEMO_DB) >/dev/null 2>&1 || true
	@docker network rm $(DEMO_NET) >/dev/null 2>&1 || true
	@echo "Demo database removed."
