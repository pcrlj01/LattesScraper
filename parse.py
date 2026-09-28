import re
import os
import json
import asyncio
import sys
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import polars as pl
from xlsxwriter import Workbook
from tqdm import tqdm
from pydantic import BaseModel, Field

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "mistralai/mistral-medium-3")

RESUMES_DIR = Path("./resumes")
JSONS_DIR = Path("./jsons")
ENRICHED_DIR = Path("./enriched_jsons")
EXCEL_DIR = Path("./excel")

for d in [JSONS_DIR, ENRICHED_DIR, EXCEL_DIR]:
    d.mkdir(parents=True, exist_ok=True)


class Publicacao(BaseModel):
    ano: int = Field(default=0, description="O ano da publicação.")
    titulo: str = Field(default="", description="O título da publicação.")
    autores: str = Field(default="", description="Lista de autores da publicação, separados por vírgula.")
    periodico: str | None = Field(default=None, description="O periódico da publicação sem a cidade.")
    doi: str | None = Field(default=None, description="O link ou identificador DOI da publicação.")
    extra: str | None = Field(default=None, description="Informações adicionais.")


class Banca(BaseModel):
    ano: int = Field(default=0, description="O ano em que a banca foi realizada.")
    titulo: str = Field(default="", description="O título do trabalho ou projeto avaliado.")
    membros: str = Field(default="", description="Lista de membros da banca, separados por vírgula.")
    candidato: str | None = Field(default=None, description="O nome do candidato avaliado.")
    instituicao: str | None = Field(default=None, description="A instituição onde a banca foi realizada.")


SYSTEM_PROMPT = (
    "Você é um assistente prestativo que extrai informações de um texto fornecido. "
    "Você receberá o texto de uma referência bibliográfica e precisará extrair as "
    "informações no formato especificado pelo esquema."
)


def extract_item_details(element) -> dict:
    """Extrai texto limpo, ano e DOI de um item span.transform do Lattes."""
    if not element:
        return {"texto": "", "ano": 0, "doi": ""}
    temp = element.__copy__()

    ano_span = temp.find("span", attrs={"data-tipo-ordenacao": "ano"})
    ano = int(ano_span.get_text(strip=True)) if (ano_span and ano_span.get_text(strip=True).isdigit()) else 0

    doi_a = temp.find("a", class_="icone-doi")
    doi = doi_a.get("href", "").strip() if doi_a else ""

    for unwanted in temp.find_all(["span", "img", "sup", "a"]):
        unwanted.decompose()

    text = re.sub(r"\s+", " ", temp.get_text(separator=" ", strip=True)).strip()

    if not ano:
        years = re.findall(r"\b(19\d\d|20\d\d)\b", text)
        ano = int(years[-1]) if years else 0

    return {"texto": text, "ano": ano, "doi": doi}


def extract_name(soup: BeautifulSoup) -> str:
    container = soup.find("div", class_="infpessoa")
    if container:
        h2 = container.find("h2", class_="nome")
        if h2:
            return h2.text.strip()
    title = soup.find("title")
    if title:
        return title.text.strip().replace("Currículo Lattes - ", "")
    return "pesquisador_desconhecido"


def extract_sections(container) -> list[dict]:
    if not container:
        return []
    sections = []
    cita_divs = container.find_all("div", class_="cita-artigos")
    for current_div in cita_divs:
        section_name = re.sub(r"\s+", " ", current_div.get_text(separator=" ", strip=True)).strip()
        if "Citações" in section_name:
            continue
        items = []
        current = current_div.next_sibling
        while current:
            if hasattr(current, "get") and current.get("class") and "cita-artigos" in current.get("class", []):
                break
            if hasattr(current, "find_all"):
                spans = current.find_all("span", class_="transform")
                for s in spans:
                    details = extract_item_details(s)
                    if details["texto"]:
                        items.append(details)
            current = current.next_sibling
        if items:
            sections.append({"name": section_name, "items": items})
    return sections


def parse_html_files() -> int:
    """Extrai texto dos HTMLs baixados para arquivos JSON brutos."""
    html_files = list(RESUMES_DIR.glob("*.html"))
    if not html_files:
        print(f"[INFO] Nenhum arquivo HTML encontrado em '{RESUMES_DIR}'.")
        return 0

    print(f"\n[1/3] Processando {len(html_files)} arquivo(s) HTML do Lattes...")
    success = 0
    for html_file in tqdm(html_files, desc="Parsing HTML"):
        try:
            with open(html_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            soup = BeautifulSoup(content, "lxml")
            name = extract_name(soup)

            pub_anchor = soup.find("a", attrs={"name": "ProducoesCientificas"})
            pub_container = pub_anchor.parent if pub_anchor else None

            comm_anchor = soup.find("a", attrs={"name": "Bancas"})
            comm_container = comm_anchor.parent if comm_anchor else None

            data = {
                "name": name,
                "lattes_id": html_file.stem,
                "publications": extract_sections(pub_container),
                "committees": extract_sections(comm_container),
            }

            out_path = JSONS_DIR / f"{html_file.stem}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            success += 1
        except Exception as e:
            print(f"  [ERRO] Falha ao processar {html_file.name}: {e}")

    print(f"✓ {success} arquivo(s) JSON brutos gerados em '{JSONS_DIR}'.")
    return success


def parse_citation_heuristic(item: dict) -> dict:
    """Extração estruturada de alta precisão baseada no formato de referências ABNT do Lattes."""
    text = item.get("texto", "")
    ano = item.get("ano", 0)
    doi = item.get("doi", "")

    if " . " in text:
        parts = text.split(" . ", 1)
        autores = parts[0].strip()
        resto = parts[1].strip()
        resto_parts = resto.split(". ", 1)
        titulo = resto_parts[0].strip()
        periodico = resto_parts[1].strip() if len(resto_parts) > 1 else ""
    elif ". " in text:
        parts = text.split(". ", 2)
        autores = parts[0].strip() if len(parts) > 0 else ""
        titulo = parts[1].strip() if len(parts) > 1 else ""
        periodico = parts[2].strip() if len(parts) > 2 else ""
    else:
        autores = ""
        titulo = text
        periodico = ""

    return {
        "ano": ano,
        "titulo": titulo,
        "autores": autores,
        "periodico": periodico,
        "doi": doi,
        "citacao_completa": text
    }


def parse_banca_heuristic(item: dict) -> dict:
    """Extração estruturada para bancas examinadoras."""
    text = item.get("texto", "")
    ano = item.get("ano", 0)

    cand_match = re.search(r"Participa[çc][ãa]o em banca (?:de|dos?)\s+([^.]+)", text, re.IGNORECASE)
    candidato = cand_match.group(1).strip() if cand_match else ""

    membros = ""
    titulo = ""
    if cand_match:
        membros = re.sub(r"\.\s*$", "", text[:cand_match.start()]).strip()
        resto = text[cand_match.end():]
        resto_parts = [p.strip() for p in re.split(r"\.\s+", resto) if p.strip()]
        titulo = resto_parts[0] if resto_parts else ""
    else:
        titulo = text[:120]

    return {
        "ano": ano,
        "titulo": titulo,
        "membros": membros,
        "candidato": candidato,
        "citacao_completa": text
    }


def enrich_all_jsons():
    """Estrutura os dados de publicações e bancas em JSONs enriquecidos."""
    json_files = list(JSONS_DIR.glob("*.json"))
    if not json_files:
        print(f"[INFO] Nenhum arquivo JSON encontrado em '{JSONS_DIR}'.")
        return

    print(f"\n[2/3] Estruturando metadados (Ano, DOI, Autores, Títulos, Bancas) para {len(json_files)} pesquisadores...")

    for json_file in tqdm(json_files, desc="Estruturando dados"):
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        enriched = {
            "name": data.get("name", "pesquisador"),
            "lattes_id": data.get("lattes_id", json_file.stem),
            "publications": [],
            "committees": []
        }

        # Process publications
        for cat in data.get("publications", []):
            cat_name = cat.get("name", "")
            items = cat.get("items", [])
            parsed_items = [parse_citation_heuristic(it) for it in items]
            enriched["publications"].append({"name": cat_name, "citations": parsed_items})

        # Process committees
        for cat in data.get("committees", []):
            cat_name = cat.get("name", "")
            items = cat.get("items", [])
            parsed_items = [parse_banca_heuristic(it) for it in items]
            enriched["committees"].append({"name": cat_name, "citations": parsed_items})

        out_path = ENRICHED_DIR / json_file.name
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(enriched, f, indent=2, ensure_ascii=False)

    print(f"✓ JSONs enriquecidos salvos em '{ENRICHED_DIR}'.")


def clean_rows(rows: list[dict]) -> list[dict]:
    cleaned = []
    for r in rows:
        row_clean = {}
        for k, v in r.items():
            if v is None:
                row_clean[k] = ""
            elif k == "ano" and str(v).isdigit():
                row_clean[k] = int(v)
            else:
                row_clean[k] = str(v)
        cleaned.append(row_clean)
    return cleaned


def export_to_excel():
    """Gera arquivos Excel individuais por pesquisador e a planilha mestre consolidada."""
    enriched_files = list(ENRICHED_DIR.glob("*.json"))
    if not enriched_files:
        enriched_files = list(JSONS_DIR.glob("*.json"))

    if not enriched_files:
        print("[INFO] Nenhum dado pronto para exportação em Excel.")
        return

    print(f"\n[3/3] Gerando planilhas Excel em '{EXCEL_DIR}'...")
    success = 0

    all_pubs_list = []
    all_comms_list = []
    summary_list = []

    for json_file in tqdm(enriched_files, desc="Exportando Excel"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_name = data.get("name", json_file.stem)
            lattes_id = data.get("lattes_id", json_file.stem)
            clean_name = re.sub(r"[^\w\s-]", "", raw_name).strip()
            slug = clean_name.lower().replace(" ", "_") if clean_name else lattes_id
            excel_path = EXCEL_DIR / f"{slug}.xlsx"

            pub_list = []
            for cat in data.get("publications", []):
                cat_name = cat.get("name", "Geral")
                for c in cat.get("citations", []):
                    item = dict(c) if isinstance(c, dict) else {"citacao_completa": str(c)}
                    item["tipo"] = cat_name
                    pub_list.append(item)

                    # Consolidated row
                    c_row = dict(item)
                    c_row["pesquisador"] = raw_name
                    c_row["lattes_id"] = lattes_id
                    all_pubs_list.append(c_row)

            comm_list = []
            for cat in data.get("committees", []):
                cat_name = cat.get("name", "Geral")
                for c in cat.get("citations", []):
                    item = dict(c) if isinstance(c, dict) else {"citacao_completa": str(c)}
                    item["tipo"] = cat_name
                    comm_list.append(item)

                    # Consolidated row
                    c_row = dict(item)
                    c_row["pesquisador"] = raw_name
                    c_row["lattes_id"] = lattes_id
                    all_comms_list.append(c_row)

            # Researcher summary
            max_pub_year = max([p.get("ano", 0) for p in pub_list if isinstance(p.get("ano"), int)], default=0)
            summary_list.append({
                "pesquisador": raw_name,
                "lattes_id": lattes_id,
                "total_publicacoes": len(pub_list),
                "total_bancas": len(comm_list),
                "ano_recente": max_pub_year if max_pub_year > 0 else ""
            })

            pub_clean = clean_rows(pub_list)
            comm_clean = clean_rows(comm_list)

            df_pub = pl.DataFrame(pub_clean, infer_schema_length=None) if pub_clean else pl.DataFrame({"tipo": [], "titulo": [], "ano": []})
            df_comm = pl.DataFrame(comm_clean, infer_schema_length=None) if comm_clean else pl.DataFrame({"tipo": [], "titulo": [], "ano": []})

            with Workbook(str(excel_path)) as wb:
                df_pub.write_excel(workbook=wb, worksheet="Publicações", autofit=True)
                df_comm.write_excel(workbook=wb, worksheet="Bancas", autofit=True)

            success += 1
        except Exception as e:
            print(f"  [ERRO] Falha ao criar Excel para {json_file.name}: {e}")

    # Generate Consolidated Master Workbook
    master_path = EXCEL_DIR / "_CONSOLIDADO_TODOS_PESQUISADORES.xlsx"
    print(f"\nGerando planilha mestre consolidada: '{master_path.name}'...")
    try:
        df_all_pubs = pl.DataFrame(clean_rows(all_pubs_list), infer_schema_length=None) if all_pubs_list else pl.DataFrame()
        df_all_comms = pl.DataFrame(clean_rows(all_comms_list), infer_schema_length=None) if all_comms_list else pl.DataFrame()
        df_summary = pl.DataFrame(clean_rows(summary_list), infer_schema_length=None) if summary_list else pl.DataFrame()

        with Workbook(str(master_path)) as wb:
            df_summary.write_excel(workbook=wb, worksheet="Resumo Pesquisadores", autofit=True)
            df_all_pubs.write_excel(workbook=wb, worksheet="Todas as Publicações", autofit=True)
            df_all_comms.write_excel(workbook=wb, worksheet="Todas as Bancas", autofit=True)

        print(f"✓ Planilha mestre criada com SUCESSO: '{master_path.name}' ({len(all_pubs_list)} publicações, {len(all_comms_list)} bancas)")
    except Exception as e:
        print(f"[ERRO] Falha ao gerar planilha mestre: {e}")

    print(f"\n✓ {success} planilha(s) Excel individual(is) gerada(s) em '{EXCEL_DIR}'!\n")


def main():
    parsed = parse_html_files()
    if parsed > 0:
        enrich_all_jsons()
        export_to_excel()
    else:
        print("\nPara executar o parsing, certifique-se de que existem arquivos .html na pasta 'resumes/'.")


if __name__ == "__main__":
    main()
