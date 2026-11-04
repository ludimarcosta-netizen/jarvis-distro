"""Read-only verification of the installed helper; never records microphone audio."""
import json
import time
import urllib.error
import urllib.request

from client import URL, client


def main():
    client.ensure()
    for _ in range(20):
        try:
            status = client.call("/status")
            if status.get("ready"):
                break
        except (OSError, ValueError):
            status = {}
        time.sleep(0.5)
    else:
        raise RuntimeError("O serviço local não ficou pronto")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for headers in ({}, {"Authorization": "Bearer invalid"},
                    {"Authorization": "Bearer " + client._token, "Origin": "https://example.com"}):
        request = urllib.request.Request(URL + "/status", data=b"{}", headers=headers, method="POST")
        try:
            opener.open(request, timeout=2).close()
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Acesso indevido ao serviço de voz")
    print(json.dumps({"ready": status["ready"], "mode": status["mode"],
                      "authentication_checks": 3, "profiles": len(status["profiles"]),
                      "candidates": len(status["candidates"])}))


if __name__ == "__main__":
    main()
