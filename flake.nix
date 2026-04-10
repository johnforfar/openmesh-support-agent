{
  description = "Openmesh Support Agent — RAG-powered docs assistant for sovereign Xnodes";

  inputs = {
    # Use the github flake URL (not the channels.nixos.org tarball) so the
    # input is a real flake and nix's input-addressable cache stays sane.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Doc corpus #1: OpenxAI documentation (45 markdown files)
    # This is the official OpenxAI docs site, packaged as a Next.js app.
    # We mount the `pages/` directory and let the backend ingest .md/.mdx
    # recursively. To pin a specific version, set `?ref=<sha>`.
    openxai-docs = {
      url = "github:OpenxAI-Network/openxai-docs";
      flake = false;
    };

    # Doc corpus #2: Openmesh CLI canonical docs (OPENMESH-SKILLS.md +
    # README + this repo's local docs/). We pull straight from johnforfar's
    # fork because that's where the latest CLI behaviour lives.
    openmesh-cli-docs = {
      url = "github:johnforfar/openmesh-cli";
      flake = false;
    };
  };

  outputs =
    inputs:
    {
      # Forkers: this is THE module to import into your own xnode container
      # config if you want to embed the support agent into a larger app.
      # Most users should just use `om app deploy` against this whole flake.
      nixosModules = {
        default =
          { pkgs, lib, ... }:
          let
            # Python environment for the RAG backend.
            # Keep this list short — every dep balloons the closure size.
            pythonEnv = pkgs.python3.withPackages (
              ps: with ps; [
                flask
                psycopg2
                requests
              ]
            );

            # Ship the backend script as a static file in the nix store so
            # systemd can ExecStart it directly.
            backendApp = pkgs.writeText "support-agent-app.py" (
              builtins.readFile ./backend/app.py
            );

            # Curated doc sources. Add more by appending paths here AND
            # giving them an `inputs.<name>` flake input above.
            #
            # Order matters for citations: earlier sources win when chunks
            # are tied on relevance.
            docsPaths = [
              "${./docs}"                              # this repo's local docs
              "${inputs.openxai-docs}/pages"           # OpenxAI Nextra pages
              "${inputs.openmesh-cli-docs}"            # openmesh-cli root (picks up OPENMESH-SKILLS.md, README.md)
            ];
          in
          {
            config = {
              # ---------------------------------------------------------------
              # 1. Ollama — chat model + embedding model
              #
              # The configuration here mirrors the canonical
              # `OpenxAI-Network/xnode-ai-chat/nixos-module.nix:48-61` setup
              # which is known to work in an xnode-manager container. The
              # `mkForce DynamicUser = false` + explicit StateDirectory is
              # the only combination that lets ollama create its own state
              # dir under the older nixpkgs that xnode-manager pins.
              # See ENGINEERING/PIPELINE-LESSONS.md Lesson #4 in openmesh-cli.
              # ---------------------------------------------------------------
              systemd.services.ollama.serviceConfig.DynamicUser = lib.mkForce false;
              systemd.services.ollama.serviceConfig.ProtectHome = lib.mkForce false;
              systemd.services.ollama.serviceConfig.StateDirectory = [ "ollama/models" ];
              services.ollama = {
                enable = true;
                user = "ollama";
                # Bind locally only; nginx is the only thing that talks to it
                host = "127.0.0.1";
                port = 11434;
                # llama3.2:1b is small enough for CPU and competent enough
                # to summarise retrieved chunks. nomic-embed-text gives
                # 768-dim embeddings via the same ollama daemon.
                loadModels = [
                  "llama3.2:1b"
                  "nomic-embed-text"
                ];
              };
              # Don't override the model-loader User/Group/DynamicUser. The
              # canonical openclaw setup leaves the model loader at its
              # nixpkgs default. Setting User=ollama here causes the loader
              # script to call `su - <model-name>` which fails with "this
              # account is currently not available". See PIPELINE-LESSONS.md
              # Lesson #10.

              # ---------------------------------------------------------------
              # 2. PostgreSQL with pgvector
              #
              # Security model: postgres binds to the local Unix socket only
              # and uses peer authentication, so the only thing that can
              # connect is a process running as the `supportagent` system
              # user. There is NO network listener, NO password, and NO
              # secret to leak. The backend systemd service below runs as
              # this same user.
              # ---------------------------------------------------------------
              users.users.supportagent = {
                isSystemUser = true;
                group = "supportagent";
              };
              users.groups.supportagent = { };

              services.postgresql = {
                enable = true;
                package = pkgs.postgresql_16;
                # The pgvector extension provides the `vector` column type
                # used by the chunks table. (`extensions` not `extraPlugins`
                # in this nixpkgs version.)
                extensions = ps: with ps; [ pgvector ];
                # Bind to the local Unix socket only — no TCP listener.
                # No password is needed because authentication is by Unix
                # peer (matching OS user to db user).
                enableTCPIP = false;
                authentication = lib.mkOverride 10 ''
                  # TYPE  DATABASE        USER            ADDRESS    METHOD
                  local   all             all                        peer
                '';
                # The backend service connects as `supportagent`. Postgres
                # creates this role and database via the ensure* options.
                ensureDatabases = [ "supportagent" ];
                ensureUsers = [
                  {
                    name = "supportagent";
                    ensureDBOwnership = true;
                  }
                ];
              };

              # Enable pgvector on every postgres startup as the postgres
              # superuser. CREATE EXTENSION requires superuser privilege,
              # which the application role (peer-auth supportagent) does
              # not have. Using `postStart` instead of `initialScript` so
              # the extension is created even on rebuilds where the data
              # dir already exists from a previous deploy.
              #
              # NOTE: openclaw/nixpkgs (Jan 2026) does NOT export $PSQL as
              # an env var inside postStart, so we use the explicit binary
              # path. See ENGINEERING/PIPELINE-LESSONS.md Lesson #5+#10.
              systemd.services.postgresql.postStart = lib.mkAfter ''
                ${pkgs.postgresql_16}/bin/psql -d supportagent -tAc 'CREATE EXTENSION IF NOT EXISTS vector;'
              '';

              # ---------------------------------------------------------------
              # 3. Backend RAG service
              # ---------------------------------------------------------------
              systemd.services.support-agent = {
                description = "Openmesh Support Agent (RAG backend)";
                after = [
                  "postgresql.service"
                  "ollama.service"
                  "network.target"
                ];
                wants = [
                  "postgresql.service"
                  "ollama.service"
                ];
                wantedBy = [ "multi-user.target" ];

                environment = {
                  # Multi-source ingestion: colon-separated list of dirs to
                  # recursively scan for .md/.mdx files.
                  DOCS_PATHS = lib.concatStringsSep ":" docsPaths;
                  # No password — postgres uses Unix socket peer auth and
                  # this systemd service runs as the `supportagent` system
                  # user, which matches the postgres role.
                  DATABASE_URL = "postgresql:///supportagent?host=/run/postgresql";
                  OLLAMA_URL = "http://127.0.0.1:11434";
                  CHAT_MODEL = "llama3.2:1b";
                  EMBED_MODEL = "nomic-embed-text";
                  EMBED_DIM = "768";
                  TOP_K = "5";
                  PORT = "5000";
                  BIND_HOST = "127.0.0.1";
                  BRAND_NAME = "Openmesh Support Agent";
                };

                serviceConfig = {
                  Type = "simple";
                  ExecStart = "${pythonEnv}/bin/python ${backendApp}";
                  Restart = "on-failure";
                  RestartSec = "10s";
                  # Run as the dedicated supportagent user so postgres peer
                  # auth (configured above) lets us connect via unix socket
                  # without any password.
                  User = "supportagent";
                  Group = "supportagent";
                  ProtectSystem = "strict";
                  ProtectHome = true;
                  PrivateTmp = true;
                };
              };

              # ---------------------------------------------------------------
              # 4. nginx — serves the static frontend on /, proxies /api/* to
              #    the Python backend on localhost:5000
              #
              # IMPORTANT: nixos-containers share the host's network namespace.
              # We MUST NOT bind port 80 (the host's nginx already owns it).
              # Internal services use 8080+ and the host reverse proxy is
              # configured (via `om app expose --port 8080`) to forward
              # public traffic from chat.<domain>:443 → support-agent.container:8080.
              # See ENGINEERING/PIPELINE-LESSONS.md Lesson #4 in openmesh-cli.
              # ---------------------------------------------------------------
              services.nginx = {
                enable = true;
                recommendedGzipSettings = true;
                recommendedOptimisation = true;
                recommendedProxySettings = true;

                virtualHosts."default" = {
                  default = true;
                  listen = [
                    {
                      addr = "0.0.0.0";
                      port = 8080;
                    }
                  ];
                  # Frontend is a single index.html with inline CSS+JS
                  root = "${./frontend}";
                  locations."/" = {
                    tryFiles = "$uri /index.html";
                  };
                  locations."/api/" = {
                    proxyPass = "http://127.0.0.1:5000";
                    extraConfig = ''
                      proxy_buffering off;
                      proxy_read_timeout 300s;
                      proxy_send_timeout 300s;
                    '';
                  };
                };
              };

              networking.firewall.allowedTCPPorts = [ 8080 ];

              # Force DHCP enablement at high priority. Something in this
              # flake (one of the service modules) is suppressing the
              # wrapper's `networking.dhcpcd.enable = true` and the dhcpcd
              # unit ends up not existing at all in the system. Forcing
              # both options explicitly here brings it back.
              # See PIPELINE-LESSONS.md Lesson #10 in openmesh-cli.
              networking.useDHCP = lib.mkForce true;
              networking.dhcpcd.enable = lib.mkForce true;
            };
          };
      };
    };
}
