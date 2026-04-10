{
  description = "Openmesh Support Agent — RAG-powered docs assistant for sovereign Xnodes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs: {
    nixosModules = {
      default =
        { pkgs, lib, ... }:
        let
          # Minimal Python env — just enough to serve a static health check
          # and prove the python service runs alongside nginx in the same
          # container. We'll add postgres + ollama back in subsequent
          # commits once the baseline is green.
          pythonEnv = pkgs.python3.withPackages (ps: with ps; [ flask ]);

          backendApp = pkgs.writeText "support-agent-app.py" ''
            from flask import Flask, jsonify
            app = Flask(__name__)

            @app.route("/api/health")
            def health():
                return jsonify({"status": "ok", "version": "v0-baseline"})

            @app.route("/api/chat", methods=["POST"])
            def chat():
                return jsonify({
                    "answer": "I am the baseline support agent. RAG features (postgres + ollama) will be added once the deploy pipeline is green.",
                    "sources": []
                })

            if __name__ == "__main__":
                print("[support-agent] starting baseline service on 127.0.0.1:5000", flush=True)
                app.run(host="127.0.0.1", port=5000)
          '';
        in
        {
          config = {
            # ---------------------------------------------------------------
            # 1. Backend service (Python Flask)
            # ---------------------------------------------------------------
            systemd.services.support-agent = {
              description = "Openmesh Support Agent (baseline)";
              after = [ "network.target" ];
              wantedBy = [ "multi-user.target" ];
              serviceConfig = {
                Type = "simple";
                ExecStart = "${pythonEnv}/bin/python ${backendApp}";
                Restart = "on-failure";
                RestartSec = "10s";
                DynamicUser = true;
              };
            };

            # ---------------------------------------------------------------
            # 2. nginx — serves the static frontend, proxies /api/* to backend
            #
            # Bind to 8080 (NOT 80) — see hello-world for the same pattern.
            # The host owns port 80; containers must use non-privileged ports.
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
