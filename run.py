#!/usr/bin/env python
"""
LattesScraper - Unified Command Line Runner
"""
import sys
import argparse
from pathlib import Path


def show_banner():
    print(r"""
======================================================
           LATTES SCRAPER & EXTRACTOR
======================================================
  [1] Scraping Lattes       (resumes.txt -> ./resumes/)
  [2] Parsing & Extração    (./resumes/ -> ./excel/ + ./jsons/)
  [3] Executar Tudo         (Scraping + Parsing + Excel)
  [4] Verificar Configuração (.env)
  [0] Sair
======================================================
""")


def check_config():
    env_file = Path(".env")
    resumes_file = Path("resumes.txt")
    resumes_dir = Path("resumes")

    # Configure stdout for UTF-8 on Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("\n--- Verificacao do Sistema ---")
    print(f"Ambiente Virtual (.venv): {'[OK] Ativo' if Path('.venv').exists() else '[X] Ausente'}")
    print(f"Arquivo .env:              {'[OK] Presente' if env_file.exists() else '[X] Ausente'}")
    print(f"Arquivo resumes.txt:       {'[OK] Presente' if resumes_file.exists() else '[X] Ausente'}")

    html_count = len(list(resumes_dir.glob("*.html"))) if resumes_dir.exists() else 0
    print(f"Curriculos em ./resumes/:  {html_count} arquivo(s) .html prontos")

    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            content = f.read()
        has_cdp = "CDP_URL=" in content and not content.split("CDP_URL=")[1].split("\n")[0].strip() == ""
        has_or = "OPENROUTER_API_KEY=" in content and not content.split("OPENROUTER_API_KEY=")[1].split("\n")[0].strip() == ""
        has_cap = "CAPSOLVER_API_KEY=" in content and not content.split("CAPSOLVER_API_KEY=")[1].split("\n")[0].strip() == ""
        print(f"CAPSOLVER_API_KEY:         {'[OK] Configurado (Resolucao automatica de CAPTCHA)' if has_cap else '[!] Nao configurado'}")
        print(f"CDP_URL (BrightData):      {'[OK] Configurado' if has_cdp else '[-] Nao configurado (usando CapSolver)'}")
        print(f"OPENROUTER_API_KEY:        {'[OK] Configurado' if has_or else '[!] Nao configurado (usara extracao heuristica automatica)'}")
    print("------------------------------\n")


def run_scraping():
    import scrape
    scrape.main()


def run_parsing():
    import parse
    parse.main()


def main():
    parser = argparse.ArgumentParser(description="LattesScraper CLI")
    parser.add_argument("--scrape", action="store_true", help="Executar o scraping dos currículos")
    parser.add_argument("--parse", action="store_true", help="Executar o parsing e geração de Excel")
    parser.add_argument("--all", action="store_true", help="Executar o pipeline completo")
    parser.add_argument("--check", action="store_true", help="Verificar status e configuração")

    args = parser.parse_args()

    if args.check:
        check_config()
        return

    if args.scrape:
        run_scraping()
        return

    if args.parse:
        run_parsing()
        return

    if args.all:
        print("\n>>> Iniciando etapa 1: Scraping...")
        run_scraping()
        print("\n>>> Iniciando etapa 2: Parsing & Excel...")
        run_parsing()
        return

    # Interactive menu
    show_banner()
    check_config()
    choice = input("Escolha uma opção (0-4): ").strip()
    if choice == "1":
        run_scraping()
    elif choice == "2":
        run_parsing()
    elif choice == "3":
        run_scraping()
        run_parsing()
    elif choice == "4":
        check_config()
    else:
        print("Saindo...")


if __name__ == "__main__":
    main()
