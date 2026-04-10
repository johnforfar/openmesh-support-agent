{
  description = "Openmesh Support Agent — incrementally adding services on top of the proven baseline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Doc corpus #1: OpenxAI documentation (45 markdown files in pages/)
    openxai-docs = {
      url = "github:OpenxAI-Network/openxai-docs";
      flake = false;
    };

    # Doc corpus #2: Openmesh CLI canonical docs
    # (OPENMESH-SKILLS.md, README.md, src layout, etc)
    openmesh-cli-docs = {
      url = "github:johnforfar/openmesh-cli";
      flake = false;
    };
  };

  outputs = inputs: {
    nixosModules = {
      default =
        { pkgs, lib, ... }:
        let
          pythonEnv = pkgs.python3.withPackages (ps: with ps; [
            flask
            psycopg2
            requests
          ]);

          # Read the full RAG backend from the repo (~330 lines).
          backendApp = pkgs.writeText "support-agent-app.py" (
            builtins.readFile ./backend/app.py
          );

          # Multi-source doc corpus paths. The python service walks these
          # at startup, chunks every .md/.mdx, embeds via nomic-embed-text,
          # and stores in postgres+pgvector.
          docsPaths = [
            "${./docs}"                              # this repo's local docs
            "${inputs.openxai-docs}/pages"           # OpenxAI Nextra pages (45 files)
            "${inputs.openmesh-cli-docs}"            # openmesh-cli root (README, SKILLS, etc)
          ];
        in
        {
          config = {
            # ===============================================================
            # Ollama — chat model + embedding model (v2)
            # Pattern from OpenxAI-Network/xnode-ai-chat/nixos-module.nix
            # ===============================================================
            systemd.services.ollama.serviceConfig.DynamicUser = lib.mkForce false;
            systemd.services.ollama.serviceConfig.ProtectHome = lib.mkForce false;
            systemd.services.ollama.serviceConfig.StateDirectory = [ "ollama/models" ];
            services.ollama = {
              enable = true;
              user = "ollama";
              host = "127.0.0.1";
              port = 11434;
              loadModels = [
                "llama3.2:1b"
                "nomic-embed-text"
              ];
            };

            # ===============================================================
            # PostgreSQL with pgvector (v1)
            # Peer auth via unix socket — no passwords, no network listener.
            # See PIPELINE-LESSONS.md Lesson #5 for the postStart pattern.
            # ===============================================================
            users.users.supportagent = {
              isSystemUser = true;
              group = "supportagent";
            };
            users.groups.supportagent = { };

            services.postgresql = {
              enable = true;
              package = pkgs.postgresql_16;
              extensions = ps: with ps; [ pgvector ];
              enableTCPIP = false;
              authentication = lib.mkOverride 10 ''
                local all all peer
              '';
              ensureDatabases = [ "supportagent" ];
              ensureUsers = [
                {
                  name = "supportagent";
                  ensureDBOwnership = true;
                }
              ];
            };

            # Enable pgvector AFTER postgresql-setup has created the
            # supportagent database. We can't use postgres.postStart for
            # this because postStart runs immediately after postgres comes
            # up, BEFORE postgresql-setup creates the database — so the
            # connection fails with "database does not exist" and the
            # postgres service is killed.
            #
            # A separate oneshot that orders after postgresql-setup is
            # the correct pattern.
            # See PIPELINE-LESSONS.md Lesson #11.
            systemd.services.support-agent-pg-init = {
              description = "Enable pgvector in supportagent database";
              after = [ "postgresql.service" "postgresql-setup.service" ];
              requires = [ "postgresql.service" ];
              wantedBy = [ "multi-user.target" ];
              serviceConfig = {
                Type = "oneshot";
                User = "postgres";
                RemainAfterExit = true;
              };
              script = ''
                ${pkgs.postgresql_16}/bin/psql -d supportagent \
                  -tAc 'CREATE EXTENSION IF NOT EXISTS vector;'
              '';
            };

            # ===============================================================
            # Backend service
            # ===============================================================
            systemd.services.support-agent = {
              description = "Openmesh Support Agent (v3-rag)";
              after = [
                "postgresql.service"
                "support-agent-pg-init.service"
                "ollama.service"
                "network.target"
              ];
              wants = [
                "postgresql.service"
                "support-agent-pg-init.service"
                "ollama.service"
              ];
              wantedBy = [ "multi-user.target" ];

              environment = {
                # Multi-source ingestion: colon-separated list of dirs the
                # python service recursively walks for .md/.mdx files.
                DOCS_PATHS = lib.concatStringsSep ":" docsPaths;
                # Peer auth via unix socket — no password.
                DATABASE_URL = "postgresql:///supportagent?host=/run/postgresql";
                OLLAMA_URL = "http://127.0.0.1:11434";
                CHAT_MODEL = "llama3.2:1b";
                EMBED_MODEL = "nomic-embed-text";
                EMBED_DIM = "768";
                # TOP_K=3 keeps the prompt small enough for llama3.2:1b on
                # CPU to respond within the host nginx 60s timeout. With
                # TOP_K=5 the prompt was ~3k tokens and inference exceeded
                # the timeout. See PIPELINE-LESSONS.md Lesson #12.
                TOP_K = "3";
                PORT = "5000";
                BIND_HOST = "127.0.0.1";
                BRAND_NAME = "Openmesh Support Agent";
              };

              serviceConfig = {
                Type = "simple";
                ExecStart = "${pythonEnv}/bin/python ${backendApp}";
                Restart = "on-failure";
                RestartSec = "10s";
                User = "supportagent";
                Group = "supportagent";
              };
            };

            # ===============================================================
            # nginx
            # ===============================================================
            services.nginx = {
              enable = true;
              recommendedGzipSettings = true;
              recommendedOptimisation = true;
              recommendedProxySettings = true;

              virtualHosts."default" = {
                default = true;
                listen = [{ addr = "0.0.0.0"; port = 8080; }];
                root = "${./frontend}";
                locations."/" = {
                  tryFiles = "$uri /index.html";
                };
                locations."/api/" = {
                  proxyPass = "http://127.0.0.1:5000";
                  extraConfig = ''
                    proxy_buffering off;
                    proxy_read_timeout 300s;
                  '';
                };
              };
            };

            networking.firewall.allowedTCPPorts = [ 8080 ];
          };
        };
    };
  };
}
