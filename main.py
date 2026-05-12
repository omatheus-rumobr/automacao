from os import getenv as carregar_env
import sys

from apurar import executar_automacao_apuracao
from login import executar_automacao_login
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

db_user = carregar_env("DB_USER", "postgres")
db_pass = carregar_env("DB_PASSWORD", "232911")
db_host = carregar_env("DB_HOST", "localhost")
db_port = carregar_env("DB_PORT", "5432")
db_name = carregar_env("DB_NAME", "api_automacoes")
database_url = f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

engine = create_engine(database_url, echo=False)
SessionLocal = sessionmaker(
    bind=engine, 
    autocommit=False, 
    autoflush=False
)

ID_STATUS_AUTOMACAO_LOGIN = 0
ID_STATUS_AGUARDANDO_EXECUCAO = 2


def principal():
    with SessionLocal() as session:
        fila_item = session.execute(
            text(
                """
                SELECT id, cnpj, data_inicio, data_fim
                FROM fila
                WHERE status_id IN (:status_login, :status_aguardando)
                ORDER BY id ASC
                LIMIT 1
                """
            ),
            {
                "status_login": ID_STATUS_AUTOMACAO_LOGIN,
                "status_aguardando": ID_STATUS_AGUARDANDO_EXECUCAO,
            },
        ).mappings().first()
        session.close()

    if fila_item["status_id"] == ID_STATUS_AUTOMACAO_LOGIN:
        executar_automacao_login(fila_item["cnpj"])

    elif fila_item["status_id"] == ID_STATUS_AGUARDANDO_EXECUCAO:
        if fila_item["automacao_id"] == 1:
            executar_automacao_apuracao()
    else:
        logger.error("Status inválido: {}", fila_item["status_id"])
        sys.exit(1)


if __name__ == "__main__":
    logger.info("O sistema iniciou com sucesso")
    # principal()
