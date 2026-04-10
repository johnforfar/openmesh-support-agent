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
          ]);

          backendApp = pkgs.writeText "support-agent-app.py" ''
            from flask import Flask, jsonify
            import os, psycopg2

            app = Flask(__name__)

            @app.route("/api/health")
            def health():
                # Confirm postgres reachable + pgvector enabled
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

                return jsonify({
                    "status": "ok",
                    "version": "v1-postgres",
                    "chat_model": "(none yet)",
                    "embed_model": "(none yet)",
                    "chunks_loaded": 0,
                    "postgres": pg_status,
                    "pgvector": pgvector,
                })

            @app.route("/api/chat", methods=["POST"])
            def chat():
                return jsonify({
                    "answer": "I am v1-postgres. PostgreSQL with pgvector is connected. Ollama and RAG features will be added next.",
                    "sources": []
                })

            if __name__ == "__main__":
                print("[support-agent] v1-postgres starting on 127.0.0.1:5000", flush=True)
                app.run(host="127.0.0.1", port=5000)
          '';
        in
        {
          config = {
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

            systemd.services.postgresql.postStart = lib.mkAfter ''
              ${pkgs.postgresql_16}/bin/psql -d supportagent -tAc 'CREATE EXTENSION IF NOT EXISTS vector;'
            '';

            # ===============================================================
            # Backend service
            # ===============================================================
            systemd.services.support-agent = {
              description = "Openmesh Support Agent (v1-postgres)";
              after = [ "postgresql.service" "network.target" ];
              wants = [ "postgresql.service" ];
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
