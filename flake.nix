{
  description = "Openmesh Support Agent — incrementally adding services on top of the proven baseline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
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

          backendApp = pkgs.writeText "support-agent-app.py" ''
            from flask import Flask, jsonify, request
            import os, psycopg2, requests as http

            app = Flask(__name__)
            OLLAMA = "http://127.0.0.1:11434"

            @app.route("/api/health")
            def health():
                # postgres
                pg_status = "unknown"
                pgvector = False
                try:
                    with psycopg2.connect("postgresql:///supportagent?host=/run/postgresql") as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT 1")
                            pg_status = "ok"
                            cur.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
                            pgvector = cur.fetchone() is not None
                except Exception as e:
                    pg_status = f"error: {str(e)[:200]}"
                # ollama
                ollama_status = "unknown"
                models = []
                try:
                    r = http.get(f"{OLLAMA}/api/tags", timeout=5)
                    r.raise_for_status()
                    models = [m.get("name","") for m in r.json().get("models",[])]
                    ollama_status = "ok"
                except Exception as e:
                    ollama_status = f"error: {str(e)[:200]}"
                return jsonify({
                    "status": "ok",
                    "version": "v2-ollama",
                    "chat_model": "llama3.2:1b",
                    "embed_model": "nomic-embed-text",
                    "chunks_loaded": 0,
                    "postgres": pg_status,
                    "pgvector": pgvector,
                    "ollama": ollama_status,
                    "models": models,
                })

            @app.route("/api/chat", methods=["POST"])
            def chat():
                data = request.get_json(force=True, silent=True) or {}
                query = (data.get("query") or "").strip()
                if not query:
                    return jsonify({"error": "empty query"}), 400
                # Direct ollama call, no RAG yet
                try:
                    r = http.post(f"{OLLAMA}/api/generate", json={
                        "model": "llama3.2:1b",
                        "prompt": query,
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 200},
                    }, timeout=300)
                    r.raise_for_status()
                    return jsonify({
                        "answer": r.json().get("response","").strip(),
                        "sources": []
                    })
                except Exception as e:
                    return jsonify({"error": str(e)}), 500

            if __name__ == "__main__":
                print("[support-agent] v2-ollama starting on 127.0.0.1:5000", flush=True)
                app.run(host="127.0.0.1", port=5000, threaded=True)
          '';
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
              description = "Openmesh Support Agent (v2-ollama)";
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
