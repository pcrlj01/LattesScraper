import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from tqdm.asyncio import tqdm_asyncio
from playwright.async_api import Playwright, async_playwright

import functools
print = functools.partial(print, flush=True)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()

import time
import requests

CDP_URL = os.environ.get("CDP_URL", "").strip()
CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY", "").strip()
CONCURRENCY = int(os.environ.get("CONCURRENCY_LIMIT", 5))

LATTES_SITE_KEY = "6LeDv6QUAAAAAP_ZK5AXrsNRQjxfbjgZLV5_YHyy"

RESUMES_DIR = Path("./resumes")
RESUMES_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_COUNTER = 0
DOWNLOAD_LOCK = asyncio.Lock()


async def solve_recaptcha_capsolver(page_url: str) -> str:
    """Resolve o reCAPTCHA v2 do Lattes automaticamente via API do CapSolver."""
    start_t = time.time()
    try:
        create_resp = requests.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": CAPSOLVER_API_KEY,
                "task": {
                    "type": "ReCaptchaV2TaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": LATTES_SITE_KEY
                }
            },
            timeout=30
        ).json()
    except Exception as e:
        raise Exception(f"Erro na conexão com CapSolver: {e}")

    task_id = create_resp.get("taskId")
    if not task_id:
        raise Exception(f"Falha ao criar task no CapSolver: {create_resp}")

    for _ in range(35):
        await asyncio.sleep(2)
        try:
            res = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={
                    "clientKey": CAPSOLVER_API_KEY,
                    "taskId": task_id
                },
                timeout=30
            ).json()
        except Exception:
            continue

        status = res.get("status")
        if status == "ready":
            token = res.get("solution", {}).get("gRecaptchaResponse")
            elapsed = time.time() - start_t
            print(f"[CAPSOLVER] Resolvido com SUCESSO em {elapsed:.1f}s!")
            return token
        elif status == "failed":
            raise Exception(f"CapSolver retornou erro: {res}")

    raise Exception("Tempo limite (70s) excedido no CapSolver")


async def scrape_single(playwright: Playwright, semaphore: asyncio.Semaphore, url: str) -> tuple[bool, str, str | None]:
    """Scrapes a single Lattes resume URL with retry."""
    async with semaphore:
        filename = url.rstrip("/").split("/")[-1]
        output_file = RESUMES_DIR / f"{filename}.html"

        if len(filename) != 16 or not filename.isdigit():
            print(f"[IGNORADO] URL com identificador Lattes inválido ({len(filename)} dígitos, esperado 16): {url}")
            return False, url, "ID Lattes inválido (deve ter 16 dígitos)"

        if output_file.exists() and output_file.stat().st_size > 1000:
            print(f"[PULADO] Já coletado anteriormente: {url} -> {output_file.name}")
            return True, url, None

        max_attempts = 2
        last_err = None

        for attempt in range(1, max_attempts + 1):
            browser = None
            try:
                if attempt > 1:
                    print(f"[RETRY #{attempt}] Tentando novamente {url}...")
                    await asyncio.sleep(2)
                else:
                    print(f"[CONECTANDO] {url}...")

                if CAPSOLVER_API_KEY:
                    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
                    if os.path.exists(chrome_path):
                        browser = await playwright.chromium.launch(executable_path=chrome_path, headless=True)
                    else:
                        browser = await playwright.chromium.launch(channel="chrome", headless=True)

                    page = await browser.new_page()
                    await page.goto(url, timeout=60_000)
                    await page.wait_for_timeout(1500)

                    is_cv = await page.query_selector("div.infpessoa")
                    if not is_cv:
                        print(f"[CAPTCHA] Resolvendo reCAPTCHA via CapSolver para {url}...")
                        token = await solve_recaptcha_capsolver(page.url)

                        print(f"[SUBMIT] Injetando token e submetendo {url}...")
                        await page.evaluate("""(token) => {
                            var el = document.getElementById('tokenCaptchar');
                            if (el) el.value = token;
                            var grecap = document.getElementById('g-recaptcha-response');
                            if (grecap) grecap.value = token;
                            var submitBtn = document.getElementById('submitBtn');
                            if (submitBtn) submitBtn.removeAttribute('disabled');

                            if (typeof verifyCallback === 'function') {
                                try { verifyCallback(token); } catch(e){}
                            }
                            if (typeof visualizarCurriculo === 'function') {
                                try { visualizarCurriculo(); } catch(e){}
                            }
                            if (submitBtn) {
                                try { submitBtn.click(); } catch(e){}
                            }
                        }""", token)

                        try:
                            await page.wait_for_selector("div.infpessoa", timeout=60_000)
                        except Exception:
                            pass

                elif CDP_URL:
                    browser = await playwright.chromium.connect_over_cdp(CDP_URL)
                    page = await browser.new_page()
                    client = await page.context.new_cdp_session(page)
                    await page.goto(url, timeout=120_000)
                    await page.wait_for_timeout(2000)

                    # Verifica se o currículo já carregou direto sem captcha
                    is_cv = await page.query_selector("div.infpessoa")
                    if not is_cv:
                        if "browserless" in CDP_URL.lower():
                            print(f"[CAPTCHA] Resolvendo reCAPTCHA automaticamente via Browserless.io para {url}...")
                            solve_res = await client.send("Browserless.solveCaptcha")
                            print(f"[CAPTCHA] Resultado: {solve_res.get('solved')}")
                            btn = await page.query_selector("#submitBtn")
                            if btn:
                                print(f"[SUBMIT] Clicando no botão submit para {url}...")
                                try:
                                    async with page.expect_navigation(timeout=45_000):
                                        await btn.click()
                                except Exception:
                                    pass
                            await page.wait_for_selector("div.infpessoa", timeout=45_000)
                        else:
                            print(f"[CAPTCHA] Aguardando resolução automática (BrightData) para {url} (até 3 min)...")
                            try:
                                result = await asyncio.wait_for(
                                    client.send(
                                        "Captcha.waitForSolve",
                                        {"detectTimeout": 15 * 1000}
                                    ),
                                    timeout=180.0
                                )
                                status = result.get("status")
                                print(f"[CAPTCHA] Status: {status} ({url})")
                                if status != "solve_finished":
                                    last_err = f"Captcha não resolvido: {status}"
                                    continue
                            except asyncio.TimeoutError:
                                print(f"[CAPTCHA] Timeout de 180s excedido na resolução para {url}. Pulando...")
                                last_err = "Timeout na resolução do CAPTCHA"
                                continue
                            except Exception as ce:
                                print(f"[CAPTCHA] Erro na resolução para {url}: {ce}")
                                last_err = f"Erro no CAPTCHA: {ce}"
                                continue

                            print(f"[SUBMIT] Clicando no botão submit para {url}...")
                            try:
                                async with page.expect_navigation(timeout=30_000):
                                    await page.click("#submitBtn")
                            except Exception:
                                try:
                                    await page.evaluate("() => { if (typeof visualizarCurriculo === 'function') { visualizarCurriculo(); } else { document.forms[0].submit(); } }")
                                except Exception:
                                    pass
                            
                            try:
                                await page.wait_for_selector("div.infpessoa", timeout=30_000)
                            except Exception:
                                pass
                else:
                    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
                    if os.path.exists(chrome_path):
                        browser = await playwright.chromium.launch(executable_path=chrome_path, headless=False)
                    else:
                        browser = await playwright.chromium.launch(channel="chrome", headless=False)

                    page = await browser.new_page()
                    print(f"[LOCAL] Abrindo {url} no navegador...")
                    await page.goto(url, timeout=60_000)

                    # Verifica se o currículo já carregou direto
                    is_cv = await page.query_selector("div.infpessoa")
                    if not is_cv:
                        print(f"\n>>> [CAPTCHA DETECTADO] Na janela que se abriu, marque 'Não sou um robô' e clique em Enviar.")
                        print(f">>> Aguardando o carregamento do currículo (até 3 minutos)...\n")
                        try:
                            await page.wait_for_selector("div.infpessoa", timeout=180_000)
                        except Exception:
                            last_err = "Tempo esgotado aguardando resolução do CAPTCHA"
                            continue

                content = await page.content()
                if "infpessoa" not in content and len(content) < 20_000:
                    last_err = "Conteúdo do currículo incompleto (possível bloqueio)"
                    continue

                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(content)

                async with DOWNLOAD_LOCK:
                    global DOWNLOAD_COUNTER
                    DOWNLOAD_COUNTER += 1
                    current_cnt = DOWNLOAD_COUNTER
                    total_in_dir = len(list(RESUMES_DIR.glob("*.html")))

                print(f"[SUCESSO #{current_cnt}] Salvo em {output_file.name} ({len(content)} bytes)")

                if current_cnt % 10 == 0:
                    print("\n" + "=" * 60)
                    print(f"🎉 MARCO ATINGIDO: {current_cnt} novos currículos baixados nesta sessão!")
                    print(f"📁 Total acumulado em ./resumes/: {total_in_dir} arquivos .html")
                    print("=" * 60 + "\n")

                return True, url, None

            except Exception as e:
                last_err = str(e)
                print(f"[AVISO] Tentativa {attempt} falhou para {url}: {last_err}")
            finally:
                if browser:
                    await browser.close()

        print(f"[ERRO FINAL] Falha definitiva ao raspar {url}: {last_err}")
        return False, url, last_err


async def run_scraper_local(playwright: Playwright, urls: list[str]) -> tuple[list[str], list[str]]:
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    print("\n[MODO LOCAL] Abrindo Google Chrome na sua tela...")
    if os.path.exists(chrome_path):
        browser = await playwright.chromium.launch(executable_path=chrome_path, headless=False)
    else:
        browser = await playwright.chromium.launch(channel="chrome", headless=False)

    context = await browser.new_context()
    page = await context.new_page()

    successful = []
    failed = []

    for i, url in enumerate(urls, 1):
        filename = url.rstrip("/").split("/")[-1]
        output_file = RESUMES_DIR / f"{filename}.html"

        if output_file.exists() and output_file.stat().st_size > 1000:
            print(f"[{i}/{len(urls)}] [PULADO] Já coletado anteriormente: {url} -> {output_file.name}")
            successful.append(url)
            continue

        print(f"\n[{i}/{len(urls)}] [LOCAL] Acessando {url}...")
        try:
            await page.goto(url, timeout=60_000)
            await page.wait_for_timeout(1000)

            is_cv = await page.query_selector("div.infpessoa")
            if not is_cv:
                print(f">>> [CAPTCHA] Na janela do Chrome aberta, marque 'Não sou um robô' e clique em Submeter.")
                print(f">>> Aguardando carregamento do currículo (até 3 minutos)...")
                try:
                    await page.wait_for_selector("div.infpessoa", timeout=180_000)
                except Exception:
                    print(f"[FALHA] Tempo limite esgotado para {url}")
                    failed.append(url)
                    continue

            content = await page.content()
            if "infpessoa" not in content and len(content) < 20_000:
                print(f"[FALHA] Conteúdo incompleto para {url}")
                failed.append(url)
                continue

            with open(output_file, "w", encoding="utf-8") as f:
                f.write(content)

            print(f"[SUCESSO] Salvo em {output_file.name} ({len(content)} bytes)")
            successful.append(url)

        except Exception as e:
            print(f"[ERRO] Falha em {url}: {e}")
            failed.append(url)

    await browser.close()
    return successful, failed


async def run_scraper(urls: list[str]) -> tuple[list[str], list[str]]:
    async with async_playwright() as playwright:
        if not CDP_URL and not CAPSOLVER_API_KEY:
            return await run_scraper_local(playwright, urls)

        already_downloaded = []
        pending_urls = []
        invalid_urls = []
        for u in urls:
            filename = u.rstrip("/").split("/")[-1]
            out = RESUMES_DIR / f"{filename}.html"
            if out.exists() and out.stat().st_size > 1000:
                already_downloaded.append(u)
            elif len(filename) != 16 or not filename.isdigit():
                invalid_urls.append(u)
            else:
                pending_urls.append(u)

        print(f"\n[STATUS] Total de URLs da lista: {len(urls)}")
        print(f"[STATUS] Já baixados anteriormente: {len(already_downloaded)}")
        if invalid_urls:
            print(f"[STATUS] URLs com formato inválido ignoradas ({len(invalid_urls)}):")
            for inv in invalid_urls:
                print(f"         - {inv} (tamanho {len(inv.rstrip('/').split('/')[-1])} dígitos)")
        print(f"[STATUS] Pendentes para baixar agora: {len(pending_urls)}\n")

        if not pending_urls:
            print("[SUCESSO] Todos os currículos válidos da lista já foram baixados e estão prontos!")
            return already_downloaded, invalid_urls

        concurrency_limit = CONCURRENCY
        semaphore = asyncio.Semaphore(concurrency_limit)
        tasks = [scrape_single(playwright, semaphore, url) for url in pending_urls]
        results = await tqdm_asyncio.gather(*tasks)

        successful = list(already_downloaded)
        failed = []
        for res in results:
            if isinstance(res, tuple):
                success, url, _ = res
                if success:
                    successful.append(url)
                else:
                    failed.append(url)
            else:
                failed.append("unknown")

        return successful, failed


def load_urls(filepath: str = "resumes.txt") -> list[str]:
    path = Path(filepath)
    if not path.exists():
        return []
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if not u or u.startswith("#"):
                continue
            # Corrige erros de digitação comuns
            if u.startswith("htttp://"):
                u = "http://" + u[8:]
            elif u.startswith("htpp://"):
                u = "http://" + u[7:]
            elif u.startswith("httpss://"):
                u = "https://" + u[9:]
            urls.append(u)
    return urls


def main():
    urls = load_urls("resumes.txt")
    if not urls:
        print("\n[AVISO] Nenhum link encontrado em 'resumes.txt'.")
        print("Adicione as URLs dos currículos Lattes em 'resumes.txt' (uma por linha).\n")
        return

    if CAPSOLVER_API_KEY:
        print(f"\n[MODO CAPSOLVER ATIVO] Resolução automática por IA.")
        print(f"Executando com concorrência={CONCURRENCY} requisições simultâneas em segundo plano.\n")
    elif not CDP_URL:
        print("\n[MODO LOCAL] CDP_URL e CapSolver não configurados.")
        print("Abrindo Google Chrome. Se o Lattes exibir o reCAPTCHA, marque 'Não sou um robô' e clique em Enviar.")
        print("O script detectará o carregamento e salvará o HTML automaticamente.\n")

    print(f"\nIniciando scraping de {len(urls)} currículo(s)...")
    successful, failed = asyncio.run(run_scraper(urls))

    print("\n" + "=" * 50)
    print(f"Resumo do Scraping:")
    print(f"  - Sucessos: {len(successful)}")
    print(f"  - Falhas:   {len(failed)}")
    print("=" * 50)

    if failed:
        with open("failed_extractions.txt", "w", encoding="utf-8") as f:
            for u in failed:
                f.write(f"{u}\n")
        print(f"URLs com falha salvas em 'failed_extractions.txt' para nova tentativa.")


if __name__ == "__main__":
    main()
