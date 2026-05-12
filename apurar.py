from datetime import date, datetime, timedelta
import json
import math
from os import getenv
import os
from pathlib import Path
import sys
from time import sleep
import traceback

import requests

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import undetected_chromedriver as uc

Path("logs").mkdir(parents=True, exist_ok=True)

logger.add(
    "logs/insercao_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="30 days",
    encoding="utf-8",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"
)

load_dotenv()

VERSAO_CHROME = int(getenv('VERSAO_CHROME'))
HEADLESS = (getenv("HEADLESS", "false") or "false").strip().lower() in ("1", "true", "yes", "y", "on")
URL_BASE = getenv('URL_BASE')
URL_DEBITOS = getenv('URL_DEBITOS')
URL_CREDITOS = getenv('URL_CREDITOS')
URL_NAO_APROVEITADOS = getenv('URL_NAO_APROVEITADOS')
URL_APURACOES = getenv('URL_APURACOES')

POR_PAGINA = int(getenv('POR_PAGINA'))

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

params_base = {
    # 'mes': MES,
    # 'ano': ANO,
    'isAposConclusao': 'false',
    'isAntesConclusao': 'true',
    #'porPagina': POR_PAGINA
}

params_creditos = {
    "idsDocumentosFiscais": "",
    # "mes": MES,
    # "ano": ANO,
    "tipoCredito": "NORMAL",
    #"porPagina": POR_PAGINA,
}

ID_AGURADANDO_EXECUCAO = 2
STATUS_ID_CONCLUIDO = 4

with SessionLocal() as session:
    fila_item = session.execute(
        text(
            """
            SELECT id, cnpj, data_inicio, data_fim
            FROM fila
            WHERE status_id = :status_id
            ORDER BY id ASC
            LIMIT 1
            """
        ),
        {"status_id": ID_AGURADANDO_EXECUCAO},
    ).mappings().first()

if not fila_item:
    print(f"Nenhum item com status_id {ID_AGURADANDO_EXECUCAO} na fila.")
    sys.exit(0)


def _parse_fila_data(val):
    if val is None:
        raise ValueError("data_inicio e data_fim são obrigatórios na fila.")
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "T" in s:
            base = s.replace("Z", "").split("+")[0].split("T")[0]
            return date.fromisoformat(base[:10])
        return date.fromisoformat(s[:10])
    raise TypeError(f"Tipo de data não suportado: {type(val)}")


_inicio = _parse_fila_data(fila_item["data_inicio"])
_fim = _parse_fila_data(fila_item["data_fim"])

if _fim < _inicio:
    _inicio, _fim = _fim, _inicio

_periodo = _fim - _inicio
_meses_vistos = set()
_cur = _inicio
mes = []
ano = []

while _cur <= _fim:
    _ym = (_cur.year, _cur.month)
    if _ym not in _meses_vistos:
        _meses_vistos.add(_ym)
        mes.append(f"{_cur.month:02d}")
        ano.append(f"{_cur.year:04d}")
    _cur = _cur + timedelta(days=1)

with SessionLocal() as session:
    cookies_db = session.execute(
        text(
            """
            SELECT domain, "httpOnly", name, path, "sameSite", secure, value
            FROM cookies
            WHERE cnpj = :cnpj
              AND deleted_at IS NULL
            """
        ),
        {"cnpj": fila_item["cnpj"]},
    ).mappings().all()


driver = None
apuracoes_por_mes = []


def executar_automacao_apuracao():
    try:
        options = uc.ChromeOptions()
        if HEADLESS:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
        
        driver = uc.Chrome(version_main=VERSAO_CHROME, options=options)
        
        driver.get(URL_BASE)
        sleep(1)
        
        if cookies_db:
            print(f"Carregando {len(cookies_db)} cookies do banco de dados")
            for cookie_db in cookies_db:
                cookie_selenium = {
                    "name": cookie_db.get("name"),
                    "value": cookie_db.get("value"),
                    "domain": cookie_db.get("domain"),
                    "path": cookie_db.get("path") or "/",
                    "secure": bool(cookie_db.get("secure", False)),
                    "httpOnly": bool(cookie_db.get("httpOnly", False))
                }

                cookie_selenium = {k: v for k, v in cookie_selenium.items() if v is not None}

                if "name" not in cookie_selenium or "value" not in cookie_selenium:
                    continue

                try:
                    driver.add_cookie(cookie_selenium)
                except Exception as e:
                    print(f"Não consegui carregar um cookie do banco: {e}")

            driver.get(URL_BASE)
        
        sleep(5)
        
        cookies_sessao = driver.get_cookies()
        cookies_dict = {}
        
        for cookie in cookies_sessao:
            cookies_dict[cookie['name']] = cookie['value']

        if len(mes) != len(ano):
            logger.error("As listas mes e ano devem ter o mesmo tamanho.")
            sys.exit(1)

        for mes_str, ano_str in zip(mes, ano):
            MES = int(mes_str)
            ANO = int(ano_str)

            params_base["mes"] = MES
            params_base["ano"] = ANO
            params_creditos["mes"] = MES
            params_creditos["ano"] = ANO
            params_creditos.pop("idApuracao", None)

            logger.info(f"Processando apurações para {mes_str}/{ano_str} (mês/ano).")
            id_apuracao = None
            apuracoes = {
                "debitos": False,
                "creditos": False,
                "nao_aproveitados": False,
            }

            if not Path(f"debitos_processados_{ano_str}_{mes_str}.json").exists():
                endpoint_url_debitos = f"{URL_BASE}{URL_DEBITOS}"
                params_primeira = params_base.copy()
                params_primeira['pagina'] = 0
                params_primeira['porPagina'] = POR_PAGINA
                
                response = requests.get(
                    url=endpoint_url_debitos, 
                    cookies=cookies_dict, 
                    params=params_primeira
                )
                logger.debug(f"Status Code: {response.status_code}")
                
                if response.status_code != 200:
                    logger.error(f"Erro na requisição: {response.text}")
                else:
                    try:
                        resultado_json = response.json()
                    except json.JSONDecodeError:
                        logger.error(f"Erro ao parsear JSON: {response.text}")
                        resultado_json = None
                    
                    if resultado_json:
                        meta = resultado_json.get('meta', {})
                        total = meta.get('total', 0)
                        existe_proxima_pagina = meta.get('existeProximaPagina', False)
                        
                        logger.info(f"Total de registros: {total}")
                        logger.info(f"Existe próxima página: {existe_proxima_pagina}")
        
                        total_paginas = math.ceil(total / 1000)
                        logger.info(f"Total de páginas a processar: {total_paginas}")
                        
                        todos_dados = []
                        
                        dados_primeira_pagina = resultado_json.get('dados', [])
                        todos_dados.extend(dados_primeira_pagina)
                        logger.info(f"Página 0 processada: {len(dados_primeira_pagina)} registros (em memória)")
                        
                        for pagina in range(1, total_paginas):
                            params = params_base.copy()
                            params['pagina'] = pagina
                            params['porPagina'] = POR_PAGINA
                            
                            logger.info(f"Fazendo requisição para página {pagina}...")
                            response = requests.get(
                                url=endpoint_url_debitos, 
                                cookies=cookies_dict, 
                                params=params
                            )
                            
                            if response.status_code != 200:
                                logger.error(f"Erro na requisição da página {pagina}: {response.text}")
                                continue
                            
                            try:
                                resultado_pagina = response.json()
                                dados_pagina = resultado_pagina.get('dados', [])
                                todos_dados.extend(dados_pagina)
                                logger.info(f"Página {pagina} processada: {len(dados_pagina)} registros (em memória)")
                            except json.JSONDecodeError:
                                logger.error(f"Erro ao parsear JSON da página {pagina}: {response.text}")
                                continue
                        
                        logger.success("Processamento concluído!")
                        logger.info(f"Total de registros coletados: {len(todos_dados)}")
                        logger.info(f"Total de páginas processadas: {total_paginas}")
        
                        if todos_dados:
                            colunas = list(todos_dados[0].keys())
                            for linha in todos_dados[1:]:
                                for chave in linha:
                                    if chave not in colunas:
                                        colunas.append(chave)
                            colunas = [c for c in colunas if c != "fila_id"]
                            colunas.append("fila_id")
                            colunas_sql = ", ".join(f'"{c}"' for c in colunas)
                            placeholders = ", ".join(f":{c}" for c in colunas)
                            insert_sql = text(
                                f"INSERT INTO debitos_processados ({colunas_sql}) VALUES ({placeholders})"
                            )
                            fila_id = fila_item["id"]
                            parametros = [
                                {**{c: linha.get(c) for c in colunas if c != "fila_id"}, "fila_id": fila_id}
                                for linha in todos_dados
                            ]
                            with SessionLocal() as session:
                                session.execute(insert_sql, parametros)
                                session.commit()
                            logger.info(
                                f"Inseridos {len(parametros)} registros em debitos_processados."
                            )
        
                            apuracoes["debitos"] = True
                        else:
                            logger.warning(
                                "Nenhum registro em todos_dados; insert em debitos_processados ignorado."
                            )
        
            if not Path(f"creditos_processados_{ano_str}_{mes_str}.json").exists():
                logger.info(f"Buscando idApuracao para {MES:02d}/{ANO}...")
                
                params_apuracoes = {
                    'pagina': 0,
                    'porPagina': 100,
                    'tipoTributo': 0
                }
                
                try:
                    response_apuracoes = requests.get(
                        url=f"{URL_BASE}{URL_APURACOES}",
                        cookies=cookies_dict,
                        params=params_apuracoes
                    )
                    
                    if response_apuracoes.status_code != 200:
                        logger.error(f"Erro na requisição de apurações: {response_apuracoes.text}")
                        id_apuracao = None
                    else:
                        resultado_apuracoes = response_apuracoes.json()
                        dados_apuracoes = resultado_apuracoes.get('dados', [])
                        id_apuracao = None
                        mes_formatado = f"{ANO}-{MES:02d}-01"
                        
                        for apuracao in dados_apuracoes:
                            data_inicial = apuracao.get('dataInicial', '')
                            if data_inicial[:7] == mes_formatado[:7]:
                                id_apuracao = apuracao.get('idApuracao')
                                situacao = apuracao.get('situacao', '')
                                valor = apuracao.get('valorApuracao', 0)
                                logger.info(f"idApuracao encontrado: {id_apuracao}")
                                logger.info(f"Situação: {situacao}")
                                logger.info(f"Valor: {valor}")
                                break
                        
                        if id_apuracao is None:
                            logger.warning(f"Nenhum idApuracao encontrado para {MES:02d}/{ANO}")
                            logger.info("Apurações disponíveis:")
                            for apuracao in dados_apuracoes:
                                logger.info(f"{apuracao.get('dataInicial')}: idApuracao={apuracao.get('idApuracao')}, situação={apuracao.get('situacao')}")
                        else:
                            params_creditos['idApuracao'] = id_apuracao
                            logger.success(f"idApuracao {id_apuracao} salvo para uso nas requisições")
                except Exception as e:
                    logger.error(f"Erro ao buscar idApuracao: {e}")
                    id_apuracao = None
                
                if id_apuracao is None:
                    logger.warning("Não foi possível obter o idApuracao. Continuando sem ele...")
                
                sleep(5)
        
                params_primeira = params_creditos.copy()
                params_primeira['pagina'] = 0
                params_primeira['porPagina'] = 10000
                
                response = requests.get(
                    url=f"{URL_BASE}{URL_CREDITOS}", 
                    cookies=cookies_dict, 
                    params=params_primeira
                )
                
                if response.status_code != 200:
                    logger.error(f"Erro na requisição: {response.text}")
                else:
                    try:
                        resultado_json_creditos = response.json()
                    except json.JSONDecodeError:
                        logger.error(f"Erro ao parsear JSON: {response.text}")
                        resultado_json_creditos = None
                    
                    if resultado_json_creditos:
                            meta = resultado_json_creditos.get('meta', {})
                            total = meta.get('total', 0)
                            existe_proxima_pagina = meta.get('existeProximaPagina', False)
                            
                            total_paginas = math.ceil(total / 1000)
                            
                            todos_dados = []
                            
                            dados_primeira_pagina = resultado_json_creditos.get('dados', [])
                            todos_dados.extend(dados_primeira_pagina)
                            logger.info(f"Página 0 processada: {len(dados_primeira_pagina)} registros (em memória)")
                            
                            for pagina in range(1, total_paginas):
                                params = params_creditos.copy()
                                params['pagina'] = pagina
                                
                                logger.info(f"Fazendo requisição para página {pagina}...")
                                response = requests.get(
                                    url=f"{URL_BASE}{URL_CREDITOS}", 
                                    cookies=cookies_dict, 
                                    params=params
                                )
                                
                                if response.status_code != 200:
                                    logger.error(f"Erro na requisição da página {pagina}: {response.text}")
                                    continue
                                
                                try:
                                    resultado_pagina = response.json()
                                    dados_pagina = resultado_pagina.get('dados', [])
                                    todos_dados.extend(dados_pagina)
                                    logger.info(f"Página {pagina} processada: {len(dados_pagina)} registros (em memória)")
                                except json.JSONDecodeError:
                                    logger.error(f"Erro ao parsear JSON da página {pagina}: {response.text}")
                                    continue
                            
                            logger.success("Processamento concluído!")
                            logger.info(f"Total de registros coletados: {len(todos_dados)}")
                            logger.info(f"Total de páginas processadas: {total_paginas}")
        
                            if todos_dados:
                                colunas = list(todos_dados[0].keys())
                                for linha in todos_dados[1:]:
                                    for chave in linha:
                                        if chave not in colunas:
                                            colunas.append(chave)
                                colunas = [c for c in colunas if c != "fila_id"]
                                colunas.append("fila_id")
                                colunas_sql = ", ".join(f'"{c}"' for c in colunas)
                                placeholders = ", ".join(f":{c}" for c in colunas)
                                insert_sql = text(
                                    f"INSERT INTO creditos_processados ({colunas_sql}) VALUES ({placeholders})"
                                )
                                fila_id = fila_item["id"]
                                parametros = [
                                    {**{c: linha.get(c) for c in colunas if c != "fila_id"}, "fila_id": fila_id}
                                    for linha in todos_dados
                                ]
                                with SessionLocal() as session:
                                    session.execute(insert_sql, parametros)
                                    session.commit()
                                logger.info(
                                    f"Inseridos {len(parametros)} registros em creditos_processados."
                                )
        
                                apuracoes["creditos"] = True
                            else:
                                logger.warning(
                                    "Nenhum registro em todos_dados; insert em creditos_processados ignorado."
                                )

            if not Path(f"nao_aproveitados_{ano_str}_{mes_str}.json").exists():
                if id_apuracao is None:
                    logger.info(f"Buscando idApuracao para {MES:02d}/{ANO} (não aproveitados)...")
                    params_apuracoes = {
                        "pagina": 0,
                        "porPagina": 100,
                        "tipoTributo": 0,
                    }
                    try:
                        response_apuracoes = requests.get(
                            url=f"{URL_BASE}{URL_APURACOES}",
                            cookies=cookies_dict,
                            params=params_apuracoes,
                        )
                        if response_apuracoes.status_code != 200:
                            logger.error(f"Erro na requisição de apurações: {response_apuracoes.text}")
                        else:
                            resultado_apuracoes = response_apuracoes.json()
                            dados_apuracoes = resultado_apuracoes.get("dados", [])
                            mes_formatado = f"{ANO}-{MES:02d}-01"
                            for apuracao in dados_apuracoes:
                                data_inicial = apuracao.get("dataInicial", "")
                                if data_inicial[:7] == mes_formatado[:7]:
                                    id_apuracao = apuracao.get("idApuracao")
                                    logger.info(f"idApuracao encontrado: {id_apuracao}")
                                    break
                            if id_apuracao is None:
                                logger.warning(f"Nenhum idApuracao encontrado para {MES:02d}/{ANO}")
                    except Exception as e:
                        logger.error(f"Erro ao buscar idApuracao: {e}")
        
                if id_apuracao is None:
                    logger.error("idApuracao ausente; não é possível buscar não aproveitados.")
                else:
                    params_nao_aproveitados_base = {
                        "idApuracao": id_apuracao,
                        "pagina": 0,
                        "porPagina": POR_PAGINA,
                    }
        
                    try:
                        response = requests.get(
                            url=f"{URL_BASE}{URL_NAO_APROVEITADOS}",
                            cookies=cookies_dict,
                            params=params_nao_aproveitados_base,
                        )
                    except Exception as e:
                        logger.error(f"Erro ao buscar nao-aproveitados: {e}")
                        response = None
        
                    if response is not None:
                        if response.status_code != 200:
                            logger.error(f"Erro na requisição: {response.text}")
                        else:
                            try:
                                resultado_json = response.json()
                            except json.JSONDecodeError:
                                logger.error(f"Erro ao parsear JSON: {response.text}")
                                resultado_json = None
        
                            if resultado_json:
                                meta = resultado_json.get("meta", {})
                                total = meta.get("total", 0)
                                total_paginas = math.ceil(total / 1000)
        
                                todos_dados = []
                                dados_primeira_pagina = resultado_json.get("dados", [])
                                todos_dados.extend(dados_primeira_pagina)
                                logger.info(
                                    f"Página 0 processada: {len(dados_primeira_pagina)} registros (em memória)"
                                )
        
                                for pagina in range(1, total_paginas):
                                    params = {
                                        "idApuracao": id_apuracao,
                                        "pagina": pagina,
                                        "porPagina": POR_PAGINA,
                                    }
                                    logger.info(f"Fazendo requisição para página {pagina}...")
                                    try:
                                        response = requests.get(
                                            url=f"{URL_BASE}{URL_NAO_APROVEITADOS}",
                                            cookies=cookies_dict,
                                            params=params,
                                        )
                                    except Exception as e:
                                        logger.error(
                                            f"Erro ao buscar nao-aproveitados página {pagina}: {e}"
                                        )
                                        continue
        
                                    if response.status_code != 200:
                                        logger.error(
                                            f"Erro na requisição da página {pagina}: {response.text}"
                                        )
                                        continue
        
                                    try:
                                        resultado_pagina = response.json()
                                        dados_pagina = resultado_pagina.get("dados", [])
                                        todos_dados.extend(dados_pagina)
                                        logger.info(
                                            f"Página {pagina} processada: {len(dados_pagina)} registros (em memória)"
                                        )
                                    except json.JSONDecodeError:
                                        logger.error(
                                            f"Erro ao parsear JSON da página {pagina}: {response.text}"
                                        )
                                        continue
        
                                logger.success("Processamento concluído!")
                                logger.info(f"Total de registros coletados: {len(todos_dados)}")
                                logger.info(f"Total de páginas processadas: {total_paginas}")
        
                                if todos_dados:
                                    colunas = list(todos_dados[0].keys())
                                    for linha in todos_dados[1:]:
                                        for chave in linha:
                                            if chave not in colunas:
                                                colunas.append(chave)
                                    colunas = [c for c in colunas if c != "fila_id"]
                                    colunas.append("fila_id")
                                    colunas_sql = ", ".join(f'"{c}"' for c in colunas)
                                    placeholders = ", ".join(f":{c}" for c in colunas)
                                    insert_sql = text(
                                        f"INSERT INTO nao_aproveitados ({colunas_sql}) VALUES ({placeholders})"
                                    )
                                    fila_id = fila_item["id"]
                                    parametros = [
                                        {**{c: linha.get(c) for c in colunas if c != "fila_id"}, "fila_id": fila_id}
                                        for linha in todos_dados
                                    ]
                                    with SessionLocal() as session:
                                        session.execute(insert_sql, parametros)
                                        session.commit()
                                    logger.info(
                                        f"Inseridos {len(parametros)} registros em nao_aproveitados."
                                    )
        
                                    apuracoes["nao_aproveitados"] = True
                                else:
                                    logger.warning(
                                        "Nenhum registro em todos_dados; insert em nao_aproveitados ignorado."
                                    )

            apuracoes_por_mes.append(dict(apuracoes))
    except Exception as e:
        logger.error(f"Erro: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
    finally:
        _esperado = len(mes)
        _todos_ciclos_ok = (
            _esperado > 0
            and len(apuracoes_por_mes) == _esperado
            and all(
                x["debitos"] and x["creditos"] and x["nao_aproveitados"]
                for x in apuracoes_por_mes
            )
        )
        if _todos_ciclos_ok:
            logger.success("Todas as apurações foram processadas com sucesso (todos os meses).")
            with SessionLocal() as session:
                session.execute(
                    text(
                        """
                        UPDATE fila
                        SET status_id = :status_id
                        WHERE id = :fila_id
                        """
                    ),
                    {
                        "status_id": STATUS_ID_CONCLUIDO, 
                        "fila_id": fila_item["id"]
                    },
                )
                session.commit()
            logger.info(f"Fila id={fila_item['id']} atualizada para status_id={STATUS_ID_CONCLUIDO} (concluído).")
        else:
            logger.info(apuracoes_por_mes)
        if driver is not None:
            driver.quit()
