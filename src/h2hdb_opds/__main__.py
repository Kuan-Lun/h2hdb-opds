import argparse
from collections.abc import Sequence

import uvicorn

from .app import create_app
from .config import load_config


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve an H2HDB catalog over OPDS 2.0")
    parser.add_argument("--config", required=True, help="Path to the OPDS JSON config")
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _argument_parser().parse_args(arguments)
    config = load_config(parsed.config)
    trusted_proxies = list(config.server.trusted_proxy_ips)
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        ssl_certfile=(
            str(config.server.tls_certificate)
            if config.server.tls_certificate is not None
            else None
        ),
        ssl_keyfile=(
            str(config.server.tls_private_key)
            if config.server.tls_private_key is not None
            else None
        ),
        proxy_headers=bool(trusted_proxies),
        forwarded_allow_ips=trusted_proxies,
    )


if __name__ == "__main__":
    main()
