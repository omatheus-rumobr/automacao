import os
from time import sleep

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import undetected_chromedriver as uc



load_dotenv()

VERSAO_CHROME = int(os.getenv('VERSAO_CHROME'))
URL_BASE = os.getenv('URL_BASE')


db_user = os.getenv("DB_USER", "postgres")
db_pass = os.getenv("DB_PASSWORD", "232911")
db_host = os.getenv("DB_HOST", "localhost")
db_port = os.getenv("DB_PORT", "5432")
db_name = os.getenv("DB_NAME", "api_automacoes")
database_url = f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

engine = create_engine(database_url, echo=False)
SessionLocal = sessionmaker(
    bind=engine, 
    autocommit=False, 
    autoflush=False
)

STATUS_ID_PENDENTE = 1
STATUS_ID_AGUARDANDO_EXECUCAO = 2
STATUS_ID_ERRO = 6


def executar_automacao_login(cnpj):
    driver = uc.Chrome(version_main=VERSAO_CHROME)
    try:
        driver.get(URL_BASE)
        driver.maximize_window()


        sleep(50)
        
        cookies = driver.get_cookies()

        with SessionLocal() as session:
            rows = []
            for cookie in cookies:
                domain = cookie.get("domain")
                name = cookie.get("name")
                value = cookie.get("value")

                if not domain or not name or value is None:
                    continue

                rows.append(
                    {
                        "domain": domain,
                        "httpOnly": bool(cookie.get("httpOnly", False)),
                        "name": name,
                        "path": cookie.get("path") or "/",
                        "sameSite": cookie.get("sameSite") or "Lax",
                        "secure": bool(cookie.get("secure", False)),
                        "value": value,
                        "cnpj": cnpj,
                    }
                )

            if rows:
                session.execute(
                    text(
                        """
                        INSERT INTO cookies (domain, "httpOnly", name, path, "sameSite", secure, value, cnpj)
                        VALUES (:domain, :httpOnly, :name, :path, :sameSite, :secure, :value, :cnpj)
                        """
                    ),
                    rows,
                )

            session.commit()

        logger.info("Cookies salvos no banco")
        
        sleep(5)
    finally:
        driver.quit()

