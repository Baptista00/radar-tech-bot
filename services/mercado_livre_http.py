"""Transporte exclusivo da API Mercado Livre, sem configuracao herdada do ambiente."""

import requests

API_TIMEOUT = (5, 20)


def create_meli_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.verify = True
    return session
