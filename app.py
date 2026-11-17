"""
Brazil Economy Monitor
----------------------
A personal Streamlit dashboard that tracks 8 indicators of Brazil's economy,
pulled live from official, free, key-less APIs:

  - Banco Central do Brasil (BCB) - SGS time series + Focus market survey
  - IBGE - SIDRA (PNAD Contínua)
  - Tesouro Nacional - Siconfi (federal RREO budget reports)

Data is cached for 7 days (weekly refresh). Use the sidebar button to force
a refresh at any time.
"""

import datetime as dt
import time
import unicodedata
import urllib.parse

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Brazil Economy Monitor", page_icon="🇧🇷", layout="wide")

CACHE_TTL = 7 * 24 * 3600  # weekly refresh
TIMEOUT = 40
HEADERS = {"User-Agent": "Mozilla/5.0 (BrazilEconomyMonitor; personal use)"}

# BCB SGS series codes (change here if you want different series)
SGS = {
    "selic": 432,        # Selic target rate, % p.a. (daily)
    "ptax": 1,           # USD/BRL PTAX, selling rate (daily)
    "ipca_12m": 13522,   # IPCA, 12-month accumulated, %
    "ibcbr": 24363,      # IBC-Br, not seasonally adjusted (used for YoY)
    "dbgg": 13762,       # General government gross debt, % of GDP
    "credit_pj": 20540,  # Credit outstanding - companies, R$ million
    "credit_pf": 20541,  # Credit outstanding - households, R$ million
    "default": 21082,    # Non-performing loans (90+ days), total, %
}

# CMN continuous inflation target (in force since 2025)
IPCA_TARGET, IPCA_TOL = 3.0, 1.5

# Federal budget functions (Portaria MOG 42/1999) - used to pick
# function-level rows (not sub-functions) from RREO Annex 2.
FUNCTIONS = {
    "Legislativa": "Legislative",
    "Judiciária": "Judiciary",
    "Essencial à Justiça": "Essential to Justice",
    "Administração": "Administration",
    "Defesa Nacional": "National Defense",
    "Segurança Pública": "Public Safety",
    "Relações Exteriores": "Foreign Relations",
    "Assistência Social": "Social Assistance",
    "Previdência Social": "Social Security (pensions)",
    "Saúde": "Health",
    "Trabalho": "Labor",
    "Educação": "Education",
    "Cultura": "Culture",
    "Direitos da Cidadania": "Citizenship Rights",
    "Urbanismo": "Urban Development",
    "Habitação": "Housing",
    "Saneamento": "Sanitation",
    "Gestão Ambiental": "Environment",
    "Ciência e Tecnologia": "Science & Technology",
    "Agricultura": "Agriculture",
    "Organização Agrária": "Land Reform",
    "Indústria": "Industry",
    "Comércio e Serviços": "Commerce & Services",
    "Comunicações": "Communications",
    "Energia": "Energy",
    "Transporte": "Transportation",
    "Desporto e Lazer": "Sports & Leisure",
    "Encargos Especiais": "Special Charges (debt service, transfers)",
}

# RREO Annex 3 revenue lines (by cod_conta) -> English label
REVENUE_LINES = {
    "ReceitaTributariaLiquidaExcetoTransferenciasEFUNDEB": "Taxes & fees (IR, IPI, IOF, II...)",
    "RREO3ReceitaDeContribuicoes": "Contributions (INSS, COFINS, CSLL, PIS...)",
    "RREO3ReceitaPatrimonial": "Property income (royalties, dividends...)",
    "RREO3ReceitaAgropecuaria": "Agricultural",
    "RREO3ReceitaIndustrial": "Industrial",
    "RREO3ReceitaDeServicos": "Services",
    "RREO3TransferenciasCorrentes": "Current transfers",
    "RREO3OutrasReceitasCorrentes": "Other current revenue",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """Upper-case, accent-free string for robust label matching."""
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in text if not unicodedata.combining(c)).upper().strip()


def _get_json(url: str, params: dict | None = None, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Request failed: {url} ({last_err})")


# ---------------------------------------------------------------------------
# Data fetchers (cached weekly)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_sgs(code: int) -> pd.Series:
    """BCB SGS series for the last ~10 years (API limit for daily series)."""
    end = dt.date.today()
    start = end - dt.timedelta(days=3640)
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
    data = _get_json(url, {
        "formato": "json",
        "dataInicial": start.strftime("%d/%m/%Y"),
        "dataFinal": end.strftime("%d/%m/%Y"),
    })
    df = pd.DataFrame(data)
    s = pd.Series(
        pd.to_numeric(df["valor"], errors="coerce").values,
        index=pd.to_datetime(df["data"], format="%d/%m/%Y"),
        name=str(code),
    ).dropna()
    return s.sort_index()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_unemployment() -> pd.Series:
    """PNAD Contínua unemployment rate (rolling quarter), IBGE SIDRA table 6381."""
    url = "https://apisidra.ibge.gov.br/values/t/6381/n1/all/v/4099/p/last%20130"
    rows = _get_json(url)[1:]  # first row is the header
    df = pd.DataFrame(rows)
    s = pd.Series(
        pd.to_numeric(df["V"], errors="coerce").values,
        index=pd.to_datetime(df["D3C"], format="%Y%m"),  # end month of rolling quarter
        name="unemployment",
    ).dropna()
    return s.sort_index()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_focus_ipca() -> pd.DataFrame:
    """Latest Focus survey median for IPCA (current and next year)."""
    base = ("https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/"
            "odata/ExpectativasMercadoAnuais")
    q = {
        "$top": "50",
        "$filter": "Indicador eq 'IPCA' and baseCalculo eq 0",
        "$orderby": "Data desc",
        "$select": "Indicador,Data,DataReferencia,Mediana,baseCalculo",
        "$format": "json",
    }
    url = base + "?" + "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in q.items())
    df = pd.DataFrame(_get_json(url)["value"])
    latest = df["Data"].max()
    df = df[df["Data"] == latest].copy()
    df["DataReferencia"] = df["DataReferencia"].astype(str)
    return df


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_rreo(year: int, period: int, annex: str) -> pd.DataFrame:
    """Federal (União) RREO annex from Tesouro's Siconfi API, all pages."""
    url = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt/rreo"
    items, offset = [], 0
    while True:
        page = _get_json(url, {
            "an_exercicio": year, "nr_periodo": period,
            "co_tipo_demonstrativo": "RREO", "no_anexo": annex,
            "co_esfera": "U", "id_ente": 1, "offset": offset,
        })
        batch = page.get("items", [])
        items.extend(batch)
        if not page.get("hasMore") or not batch:
            break
        offset += len(batch)
    return pd.DataFrame(items)


def latest_rreo(annex: str):
    """Most recent bimester (1-6) with data, searching this year then last year."""
    this_year = dt.date.today().year
    for year in (this_year, this_year - 1):
        for period in range(6, 0, -1):
            df = fetch_rreo(year, period, annex)
            if not df.empty:
                return year, period, df
    raise RuntimeError(f"No RREO data found for {annex}")


# ---------------------------------------------------------------------------
# Transformations
# ---------------------------------------------------------------------------
def monthly_last(s: pd.Series) -> pd.Series:
    return s.resample("MS").last().dropna()


def yoy(s: pd.Series) -> pd.Series:
    """Year-over-year % change for a monthly series."""
    s = s.resample("MS").last()
    return (s / s.shift(12) - 1).mul(100).dropna()


def value_ago(s: pd.Series, days: int) -> float:
    target = s.index[-1] - pd.Timedelta(days=days)
    prior = s[s.index <= target]
    return float(prior.iloc[-1]) if not prior.empty else float("nan")


def spending_by_function(df: pd.DataFrame):
    """Function-level federal spending from RREO Annex 2 (R$ billion)."""
    cols = df["coluna"].dropna().unique()
    chosen = None
    labels = {
        "LIQUIDADAS ATE O BIMESTRE": "liquidated (executed) spending, year to date",
        "EMPENHADAS ATE O BIMESTRE": "committed spending, year to date",
        "DOTACAO ATUALIZADA": "authorized budget for the year",
    }
    for pattern, label in labels.items():
        match = [c for c in cols if pattern in _norm(c)]
        if match:
            chosen = match[0]
            break
    if chosen is None:
        raise RuntimeError("Expected spending column not found in RREO Annex 2")
    sub = df[(df["coluna"] == chosen) & (df["conta"].isin(FUNCTIONS.keys()))]
    # First occurrence = regular budget (later repeats are intra-budget items)
    sub = sub.drop_duplicates(subset="conta", keep="first")
    out = pd.DataFrame({
        "Function": sub["conta"].map(FUNCTIONS),
        "R$ billion": pd.to_numeric(sub["valor"], errors="coerce") / 1e9,
    }).dropna()
    return label, out.sort_values("R$ billion", ascending=False)


def revenue_by_type(df: pd.DataFrame):
    """12-month federal current revenue by type from RREO Annex 3 (R$ billion)."""
    col = [c for c in df["coluna"].dropna().unique() if "ULTIMOS 12 MESES" in _norm(c)]
    if not col:
        raise RuntimeError("12-month column not found in RREO Annex 3")
    sub = df[df["coluna"] == col[0]].drop_duplicates(subset="cod_conta", keep="first")
    vals = pd.to_numeric(sub.set_index("cod_conta")["valor"], errors="coerce") / 1e9
    rev = pd.DataFrame({
        "Source": [REVENUE_LINES[k] for k in REVENUE_LINES if k in vals.index],
        "R$ billion": [vals[k] for k in REVENUE_LINES if k in vals.index],
    })
    rev = rev[rev["R$ billion"] > 0].sort_values("R$ billion", ascending=False)
    extra = {
        "transfers": vals.get("ReceitaDeTransferenciasConstitucionaisELegais"),
        "rcl": vals.get("RREO3ReceitaCorrenteLiquida"),
    }
    return rev, extra


# ---------------------------------------------------------------------------
# Theme (Kellogg purple)
# ---------------------------------------------------------------------------
KELLOGG = "#4E2A84"        # Northwestern / Kellogg purple (UI chrome)
KELLOGG_DARK = "#401F68"
KELLOGG_TINT = "#E4E0EE"
KELLOGG_SOFT = "#F4F1F9"
SERIES_1 = "#5b3b9a"       # chart purple (validated for contrast & color-blind safety)
SERIES_2 = "#eb6834"       # second series (orange)
INK, INK_2, INK_3 = "#1d1a24", "#55505f", "#8a8594"
GRID = "#ECE9F2"

STATUS = {  # color + icon + label, never color alone
    "good": ("#0ca30c", "#E7F6E7", "✓"),
    "warning": ("#b07800", "#FEF4DC", "!"),
    "serious": ("#c4542a", "#FCEBE3", "▲"),
    "critical": ("#d03b3b", "#FBE6E6", "✕"),
    "neutral": (KELLOGG, KELLOGG_TINT, "●"),
}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stApp {{ font-family: 'Inter', sans-serif; }}
.stApp {{ background: #FBFAFD; color: {INK}; }}
header[data-testid="stHeader"] {{ background: transparent; }}
section[data-testid="stSidebar"] {{ background: {KELLOGG}; }}
section[data-testid="stSidebar"] * {{ color: #FFFFFF !important; }}
section[data-testid="stSidebar"] button {{ background: #FFFFFF22; border: 1px solid #FFFFFF55; }}
.block-container {{ padding-top: 1.6rem; max-width: 1300px; }}

.hero {{ background: linear-gradient(120deg, {KELLOGG_DARK} 0%, {KELLOGG} 60%, #6b4fa8 100%);
        border-radius: 16px; padding: 26px 30px; color: #fff; margin-bottom: 18px; }}
.hero h1 {{ color: #fff; font-size: 2.0rem; font-weight: 700; margin: 0; padding: 0; }}
.hero p {{ color: #E4E0EE; margin: 6px 0 0 0; font-size: 0.98rem; }}

.section-title {{ font-size: 1.15rem; font-weight: 700; color: {KELLOGG}; margin: 10px 0 8px 0;
                 border-bottom: 2px solid {KELLOGG_TINT}; padding-bottom: 6px; }}

.summary {{ background: #fff; border: 1px solid {KELLOGG_TINT}; border-left: 5px solid {KELLOGG};
           border-radius: 12px; padding: 14px 18px; margin-bottom: 18px; }}
.summary ul {{ margin: 0; padding-left: 0; list-style: none; }}
.summary li {{ margin: 7px 0; color: {INK}; font-size: 0.95rem; line-height: 1.45; }}

.kpi {{ background: #fff; border: 1px solid {KELLOGG_TINT}; border-radius: 12px; padding: 14px 16px;
       height: 100%; box-shadow: 0 1px 2px rgba(78,42,132,0.06); }}
.kpi .label {{ font-size: 0.78rem; font-weight: 600; color: {INK_2}; text-transform: uppercase;
              letter-spacing: 0.03em; }}
.kpi .value {{ font-size: 1.75rem; font-weight: 700; color: {INK}; margin: 4px 0 2px 0; }}
.kpi .delta {{ font-size: 0.82rem; color: {INK_2}; margin-bottom: 8px; }}
.kpi .asof {{ font-size: 0.72rem; color: {INK_3}; margin-top: 6px; }}

.chip {{ display: inline-block; font-size: 0.76rem; font-weight: 600; padding: 3px 9px;
        border-radius: 999px; white-space: nowrap; }}

.card-head {{ margin: 4px 0 2px 0; }}
.card-head .t {{ font-size: 1.02rem; font-weight: 700; color: {INK}; }}
.why {{ font-size: 0.88rem; color: {INK_2}; line-height: 1.45; margin: 4px 0 2px 0; }}
.read {{ font-size: 0.82rem; color: {INK_2}; background: {KELLOGG_SOFT}; border-radius: 8px;
        padding: 8px 12px; margin: -6px 0 18px 0; line-height: 1.45; }}
.read b {{ color: {KELLOGG}; }}

.stTabs [data-baseweb="tab-list"] {{ gap: 6px; }}
.stTabs [data-baseweb="tab"] {{ background: #fff; border: 1px solid {KELLOGG_TINT};
        border-radius: 10px 10px 0 0; padding: 8px 16px; font-weight: 600; color: {INK_2}; }}
.stTabs [aria-selected="true"] {{ background: {KELLOGG}; color: #fff !important; border-color: {KELLOGG}; }}
.stTabs [data-baseweb="tab-highlight"], .stTabs .react-aria-SelectionIndicator {{ background: {KELLOGG} !important; }}
div[data-testid="stExpander"] {{ background: #fff; border-radius: 10px; }}
section[data-testid="stSidebar"] .chip {{ color: {INK} !important; background: #FFFFFF !important; }}
/* Recolor Streamlit's default red accent */
section[data-testid="stSidebar"] div[data-testid="stSlider"] div[role="group"] {{ filter: grayscale(1) brightness(2.2); }}
div[data-testid="stCheckbox"] label > div:first-of-type {{ filter: hue-rotate(262deg) saturate(0.75) brightness(0.8); }}
div[data-testid="stDownloadButton"] button:hover, div[data-testid="stButton"] button:hover {{ border-color: {KELLOGG}; color: {KELLOGG}; }}
</style>
"""


def chip(status: str, label: str) -> str:
    color, bg, icon = STATUS[status]
    return (f'<span class="chip" style="color:{color};background:{bg};'
            f'border:1px solid {color}33">{icon} {label}</span>')


# ---------------------------------------------------------------------------
# Indicator explanations ("why it matters" / "how to read it")
# ---------------------------------------------------------------------------
EXPLAIN = {
    "ibc": dict(
        why="The Central Bank's monthly proxy for GDP. It is the earliest broad read on whether "
            "Brazil's economy is growing, available well before quarterly GDP.",
        how="Above 0% means the economy is larger than a year ago. Single months are noisy, "
            "so watch the direction of the line over several months."),
    "ipca": dict(
        why="The official inflation index and the Central Bank's target variable. It drives Selic "
            "decisions and erodes household purchasing power.",
        how=f"The shaded area is the tolerance band ({IPCA_TARGET - IPCA_TOL:.1f}%–"
            f"{IPCA_TARGET + IPCA_TOL:.1f}%). Under the continuous-target regime, six consecutive "
            "months outside the band count as a missed target. The Focus survey shows where the "
            "market expects inflation to land."),
    "selic": dict(
        why="The Central Bank's policy rate. It sets the cost of credit across the economy and "
            "makes Brazilian assets attractive to foreign investors (the carry trade).",
        how="What matters is the real rate: Selic minus expected inflation. A high real rate cools "
            "demand and inflation, but also weighs on credit, investment and public debt costs."),
    "fx": dict(
        why="Brazil is a commodity exporter with open capital markets, so the real reacts to "
            "global risk appetite, commodity prices and domestic fiscal credibility.",
        how="Up means a weaker real (more reais per dollar). A sustained depreciation feeds into "
            "inflation through import prices, so the Central Bank watches it closely."),
    "unemp": dict(
        why="The broadest read on household health. Jobs drive consumption, which is roughly "
            "two-thirds of GDP, and a tight labor market pushes up wages and services inflation.",
        how="It is a rolling 3-month average and seasonal, so compare it with the same period a "
            "year earlier rather than with last month."),
    "debt": dict(
        why="The main gauge of fiscal sustainability. When debt rises, markets demand a higher "
            "premium, which shows up in long-term rates and a weaker real.",
        how="Debt/GDP rises when deficits plus interest costs outpace nominal GDP growth. "
            "The trend matters more than the level."),
    "credit": dict(
        why="Shows whether the private sector is getting financing. Credit funds household "
            "consumption and business investment, and it is the main channel through which a high "
            "Selic slows the economy.",
        how="Growth here is nominal, so compare it with IPCA: growth above inflation means real "
            "expansion. Companies vs. households shows who is borrowing."),
    "npl": dict(
        why="The share of loans more than 90 days overdue. It is the clearest stress signal for "
            "households, companies and banks.",
        how="A rising default rate usually lags high interest rates by a few quarters, and it "
            "leads banks to tighten lending."),
    "revenue": dict(
        why="Shows what the federal budget is funded by: classic taxes (income tax, IPI, IOF, "
            "import duties) vs. social contributions (INSS payroll, COFINS, PIS, CSLL), which are "
            "largely earmarked.",
        how="A large share of tax revenue is constitutionally transferred to states and "
            "municipalities, so the federal government keeps less than the headline total."),
    "spending": dict(
        why="Shows where federal money goes, and how rigid the budget is. Pensions, health, "
            "education and social assistance are largely mandatory, which leaves little room "
            "for discretionary spending.",
        how="'Special Charges' (debt interest, debt refinancing and transfers) is hidden by "
            "default because refinancing inflates it. Tick the box to include it."),
}


# ---------------------------------------------------------------------------
# Signals: turn each latest reading into a status + one-line interpretation
# ---------------------------------------------------------------------------
def sig_ibc(s):
    v = s.iloc[-1]
    trend = s.iloc[-3:].mean() - s.iloc[-6:-3].mean() if len(s) >= 6 else 0
    direction = "accelerating" if trend > 0.3 else "slowing" if trend < -0.3 else "stable"
    if v < 0:
        return "serious", "Contracting", f"Activity is {abs(v):.1f}% below a year ago and {direction}."
    if v < 1.5:
        return "warning", "Slow growth", f"Activity is up {v:.1f}% YoY, with momentum {direction}."
    return "good", "Expanding", f"Activity is up {v:.1f}% YoY, with momentum {direction}."


def sig_ipca(s, focus_now):
    v = s.iloc[-1]
    lo, hi = IPCA_TARGET - IPCA_TOL, IPCA_TARGET + IPCA_TOL
    months_out = 0
    for x in reversed(s.values):
        if lo <= x <= hi:
            break
        months_out += 1
    exp = f" Market expects {focus_now:.2f}% for this year." if focus_now else ""
    if v > hi:
        return ("serious", "Above ceiling",
                f"Inflation at {v:.2f}% is above the {hi:.1f}% ceiling "
                f"({months_out} month{'s' if months_out != 1 else ''} in a row).{exp}")
    if v < lo:
        return "warning", "Below floor", f"Inflation at {v:.2f}% is below the {lo:.1f}% floor.{exp}"
    if v > IPCA_TARGET + 0.5:
        return "warning", "Inside band, above target", f"Inflation at {v:.2f}% is inside the band but above the {IPCA_TARGET}% target.{exp}"
    return "good", "Near target", f"Inflation at {v:.2f}% is close to the {IPCA_TARGET}% target.{exp}"


def sig_selic(s, focus_next):
    v = s.iloc[-1]
    chg = v - value_ago(s, 120)
    real = ((1 + v / 100) / (1 + focus_next / 100) - 1) * 100 if focus_next else None
    real_txt = f" Ex-ante real rate ≈ {real:.1f}%." if real is not None else ""
    if chg > 0.1:
        return "warning", "Hiking cycle", f"Selic at {v:.2f}%, up {chg:.2f} pp in 4 months.{real_txt}"
    if chg < -0.1:
        return "good", "Easing cycle", f"Selic at {v:.2f}%, down {abs(chg):.2f} pp in 4 months.{real_txt}"
    return "neutral", "On hold", f"Selic unchanged at {v:.2f}% over the last 4 months.{real_txt}"


def sig_fx(s):
    v = s.iloc[-1]
    chg = (v / value_ago(s, 30) - 1) * 100
    if chg > 3:
        return "serious", "Real weakening", f"US$1 = R${v:.2f}; the real lost {chg:.1f}% in 30 days."
    if chg < -3:
        return "good", "Real strengthening", f"US$1 = R${v:.2f}; the real gained {abs(chg):.1f}% in 30 days."
    return "neutral", "Stable", f"US$1 = R${v:.2f} ({chg:+.1f}% in 30 days)."


def sig_unemp(s):
    v, d = s.iloc[-1], s.iloc[-1] - s.iloc[-13] if len(s) > 12 else 0
    if d <= -0.3:
        return "good", "Improving", f"Unemployment at {v:.1f}%, {abs(d):.1f} pp lower than a year ago."
    if d >= 0.3:
        return "serious", "Deteriorating", f"Unemployment at {v:.1f}%, {d:.1f} pp higher than a year ago."
    return "neutral", "Stable", f"Unemployment at {v:.1f}%, roughly unchanged from a year ago."


def sig_debt(s):
    v, d = s.iloc[-1], s.iloc[-1] - s.iloc[-13] if len(s) > 12 else 0
    if d >= 1:
        return "serious", "Rising", f"Gross debt at {v:.1f}% of GDP, up {d:.1f} pp in 12 months."
    if d <= -1:
        return "good", "Falling", f"Gross debt at {v:.1f}% of GDP, down {abs(d):.1f} pp in 12 months."
    return "warning", "Flat", f"Gross debt at {v:.1f}% of GDP ({d:+.1f} pp in 12 months)."


def sig_credit(credit_yoy, ipca):
    v = credit_yoy.iloc[-1]
    real = v - ipca.iloc[-1] if ipca is not None else None
    real_txt = f" (≈ {real:+.1f}% after inflation)" if real is not None else ""
    if real is not None and real < 0:
        return "serious", "Shrinking in real terms", f"Credit grows {v:.1f}% YoY{real_txt}."
    if real is not None and real < 3:
        return "warning", "Modest", f"Credit grows {v:.1f}% YoY{real_txt}."
    return "good", "Expanding", f"Credit grows {v:.1f}% YoY{real_txt}."


def sig_npl(s):
    v, d = s.iloc[-1], s.iloc[-1] - value_ago(s, 365)
    if d >= 0.3:
        return "serious", "Rising stress", f"Defaults at {v:.2f}% of loans, up {d:.2f} pp in a year."
    if d <= -0.3:
        return "good", "Easing", f"Defaults at {v:.2f}% of loans, down {abs(d):.2f} pp in a year."
    return "neutral", "Stable", f"Defaults at {v:.2f}% of loans ({d:+.2f} pp in a year)."


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------
def _base_layout(fig, ytitle, height, legend):
    fig.update_layout(
        template="plotly_white", height=height, paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
        font=dict(family="Inter, sans-serif", color=INK_2, size=12),
        margin=dict(l=8, r=58, t=30 if legend else 12, b=8), hovermode="x unified",
        yaxis=dict(title=ytitle, gridcolor=GRID, zeroline=False, ticks=""),
        xaxis=dict(showgrid=False, linecolor=GRID),
        showlegend=legend,
        legend=dict(orientation="h", y=1.02, x=0, yanchor="bottom", font_color=INK_2),
        hoverlabel=dict(bgcolor="#FFFFFF", bordercolor=KELLOGG_TINT, font_color=INK),
    )
    return fig


def line_chart(series: dict, ytitle: str, start, band=None, hline=None, zero=False,
               fmt_last="{:.1f}", height=300):
    fig = go.Figure()
    if band is not None:
        fig.add_hrect(y0=band[0], y1=band[1], fillcolor=KELLOGG, opacity=0.07, line_width=0)
    if hline is not None:
        fig.add_hline(y=hline[0], line_dash="dot", line_color=INK_3, line_width=1,
                      annotation_text=hline[1], annotation_position="top left",
                      annotation_font=dict(color=INK_3, size=11))
    if zero:
        fig.add_hline(y=0, line_color="#CFCBD8", line_width=1)
    colors = [SERIES_1, SERIES_2]
    for i, (name, s) in enumerate(series.items()):
        s = s[s.index >= start]
        if s.empty:
            continue
        c = colors[i % 2]
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values, name=name, mode="lines", line=dict(color=c, width=2.2),
            fill="tozeroy" if (len(series) == 1 and zero is False and band is None and s.min() > 0
                               and s.min() / max(s.max(), 1e-9) < 0.5) else None,
            fillcolor="rgba(91,59,154,0.06)",
            hovertemplate=f"{name}: %{{y:.2f}}<extra></extra>",
        ))
        # Emphasize the latest point with a marker + direct label
        fig.add_trace(go.Scatter(
            x=[s.index[-1]], y=[s.iloc[-1]], mode="markers+text", showlegend=False,
            marker=dict(size=9, color=c, line=dict(color="#FFFFFF", width=2)),
            text=[fmt_last.format(s.iloc[-1])], textposition="middle right",
            textfont=dict(color=INK, size=12), hoverinfo="skip", cliponaxis=False,
        ))
    return _base_layout(fig, ytitle, height, legend=len(series) > 1)


def bar_chart(df: pd.DataFrame, label: str):
    df = df.sort_values("R$ billion")
    total = df["R$ billion"].sum()
    fig = go.Figure(go.Bar(
        x=df["R$ billion"], y=df[label], orientation="h",
        marker=dict(color=SERIES_1, cornerradius=4),
        text=[f"R$ {v:,.0f} bn · {v / total:.0%}" for v in df["R$ billion"]],
        textposition="outside", textfont=dict(color=INK_2, size=11), cliponaxis=False,
        hovertemplate="%{y}: R$ %{x:,.1f} bn<extra></extra>",
    ))
    fig = _base_layout(fig, None, max(280, 30 * len(df) + 40), legend=False)
    fig.update_layout(hovermode="closest", margin=dict(l=8, r=110, t=10, b=8), bargap=0.35)
    fig.update_xaxes(showgrid=True, gridcolor=GRID, title="R$ billion")
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)", title=None)
    return fig


def card_head(container, title, key, signal=None):
    chip_html = chip(signal[0], signal[1]) if signal else ""
    container.markdown(
        f'<div class="card-head"><span class="t">{title}</span>&nbsp;&nbsp;{chip_html}</div>'
        f'<div class="why">{EXPLAIN[key]["why"]}</div>', unsafe_allow_html=True)


def card_foot(container, key, signal=None):
    now = f"<b>Now:</b> {signal[2]}<br>" if signal else ""
    container.markdown(f'<div class="read">{now}<b>How to read it:</b> {EXPLAIN[key]["how"]}</div>',
                       unsafe_allow_html=True)


def show(container, fig):
    container.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False})


def safe(label: str, fn, *args):
    try:
        return fn(*args)
    except Exception as err:  # noqa: BLE001
        st.warning(f"Could not load **{label}** right now ({err}). Try the refresh button later.")
        return None


def safe_signal(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def fmt(v, suffix="", decimals=1):
    return "n/a" if v is None or pd.isna(v) else f"{v:,.{decimals}f}{suffix}"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.markdown(CSS, unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### ⚙️ Settings")
    years = st.slider("History shown (years)", 1, 9, 5)
    if st.button("🔄 Refresh data now", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Data is cached and refreshed automatically every 7 days.")
    st.divider()
    st.markdown("**Sources**")
    st.caption("Banco Central do Brasil (SGS, Focus survey) · IBGE (SIDRA, PNAD Contínua) · "
               "Tesouro Nacional (Siconfi, RREO)")
    st.divider()
    st.markdown("**Status tags**")
    st.markdown(" ".join(chip(k, l) for k, l in [("good", "Favorable"), ("warning", "Watch"),
                                                   ("serious", "Concern"), ("neutral", "Neutral")]),
                unsafe_allow_html=True)

start = pd.Timestamp(dt.date.today()) - pd.DateOffset(years=years)

st.markdown(
    f'<div class="hero"><h1>🇧🇷 Brazil Economy Monitor</h1>'
    f'<p>Eight indicators covering growth, inflation, rates, the real, jobs, public debt, private credit '
    f'and federal taxes · updated {dt.date.today():%b %d, %Y}</p></div>',
    unsafe_allow_html=True)

with st.spinner("Loading data from BCB, IBGE and Tesouro..."):
    sgs = {k: safe(f"SGS {code}", fetch_sgs, code) for k, code in SGS.items()}
    unemp = safe("Unemployment (IBGE)", fetch_unemployment)
    focus = safe("Focus survey (BCB)", fetch_focus_ipca)
    rreo2 = safe("Federal spending (Tesouro)", latest_rreo, "RREO-Anexo 02")
    rreo3 = safe("Federal revenue (Tesouro)", latest_rreo, "RREO-Anexo 03")

ibc_yoy = yoy(sgs["ibcbr"]) if sgs["ibcbr"] is not None else None
selic_m = monthly_last(sgs["selic"]) if sgs["selic"] is not None else None
ptax_m = monthly_last(sgs["ptax"]) if sgs["ptax"] is not None else None
npl_m = monthly_last(sgs["default"]) if sgs["default"] is not None else None
credit_pj_yoy = yoy(sgs["credit_pj"]) if sgs["credit_pj"] is not None else None
credit_pf_yoy = yoy(sgs["credit_pf"]) if sgs["credit_pf"] is not None else None
credit_total_yoy = (
    yoy(sgs["credit_pj"].add(sgs["credit_pf"]).dropna())
    if sgs["credit_pj"] is not None and sgs["credit_pf"] is not None else None
)

this_year, next_year = str(dt.date.today().year), str(dt.date.today().year + 1)
focus_now = focus_next = None
if focus is not None:
    f_map = dict(zip(focus["DataReferencia"], focus["Mediana"]))
    focus_now, focus_next = f_map.get(this_year), f_map.get(next_year)

revenue, rev_extra, spending, spend_col = None, {}, None, None
if rreo3 is not None:
    res = safe("Federal revenue breakdown", revenue_by_type, rreo3[2])
    if res:
        revenue, rev_extra = res
if rreo2 is not None:
    res = safe("Federal spending breakdown", spending_by_function, rreo2[2])
    if res:
        spend_col, spending = res

# Signals
sig = {
    "ibc": safe_signal(sig_ibc, ibc_yoy) if ibc_yoy is not None else None,
    "ipca": safe_signal(sig_ipca, sgs["ipca_12m"], focus_now) if sgs["ipca_12m"] is not None else None,
    "selic": safe_signal(sig_selic, sgs["selic"], focus_next or focus_now) if sgs["selic"] is not None else None,
    "fx": safe_signal(sig_fx, sgs["ptax"]) if sgs["ptax"] is not None else None,
    "unemp": safe_signal(sig_unemp, unemp) if unemp is not None else None,
    "debt": safe_signal(sig_debt, sgs["dbgg"]) if sgs["dbgg"] is not None else None,
    "credit": safe_signal(sig_credit, credit_total_yoy, sgs["ipca_12m"]) if credit_total_yoy is not None else None,
    "npl": safe_signal(sig_npl, sgs["default"]) if sgs["default"] is not None else None,
}

# ---------------------------------------------------------------------------
# This week's read (auto-generated summary)
# ---------------------------------------------------------------------------
st.markdown('<div class="section-title">This week\'s read</div>', unsafe_allow_html=True)
names = {"ibc": "Growth", "ipca": "Inflation", "selic": "Monetary policy", "fx": "Currency",
         "unemp": "Jobs", "debt": "Public debt", "credit": "Private credit", "npl": "Credit quality"}
items = "".join(f"<li>{chip(s[0], s[1])}&nbsp; <b>{names[k]}:</b> {s[2]}</li>"
                for k, s in sig.items() if s)
if items:
    st.markdown(f'<div class="summary"><ul>{items}</ul></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# KPI cards (8 indicators)
# ---------------------------------------------------------------------------
def kpi(col, label, value, delta, signal, asof):
    chip_html = chip(signal[0], signal[1]) if signal else ""
    col.markdown(
        f'<div class="kpi"><div class="label">{label}</div><div class="value">{value}</div>'
        f'<div class="delta">{delta or "&nbsp;"}</div>{chip_html}'
        f'<div class="asof">{asof or "&nbsp;"}</div></div>', unsafe_allow_html=True)


def last_delta(s, lag):
    if s is None or len(s) <= lag:
        return None, None, None
    return float(s.iloc[-1]), float(s.iloc[-1] - s.iloc[-1 - lag]), s.index[-1]


st.markdown('<div class="section-title">Snapshot</div>', unsafe_allow_html=True)
r1, r2 = st.columns(4), st.columns(4)

v, d, t = last_delta(ibc_yoy, 1)
kpi(r1[0], "Activity · IBC-Br YoY", fmt(v, "%"), d is not None and f"{d:+.1f} pp vs prior month",
    sig["ibc"], t is not None and f"Latest: {t:%b %Y}")
v, d, t = last_delta(sgs["ipca_12m"], 1)
kpi(r1[1], "Inflation · IPCA 12m", fmt(v, "%", 2), d is not None and f"{d:+.2f} pp vs prior month",
    sig["ipca"], t is not None and f"Latest: {t:%b %Y} · target {IPCA_TARGET}%")
s = sgs["selic"]
kpi(r1[2], "Policy rate · Selic", fmt(s.iloc[-1] if s is not None else None, "%", 2),
    s is not None and f"{s.iloc[-1] - value_ago(s, 365):+.2f} pp vs 1 year ago",
    sig["selic"], s is not None and f"As of {s.index[-1]:%b %d, %Y}")
s = sgs["ptax"]
kpi(r1[3], "Currency · USD/BRL", fmt(s.iloc[-1] if s is not None else None, "", 3),
    s is not None and f"{(s.iloc[-1] / value_ago(s, 30) - 1) * 100:+.1f}% vs 30 days ago",
    sig["fx"], s is not None and f"As of {s.index[-1]:%b %d, %Y}")
v, d, t = last_delta(unemp, 12)
kpi(r2[0], "Jobs · Unemployment", fmt(v, "%"), d is not None and f"{d:+.1f} pp vs 1 year ago",
    sig["unemp"], t is not None and f"Quarter ending {t:%b %Y}")
v, d, t = last_delta(sgs["dbgg"], 12)
kpi(r2[1], "Fiscal · Gross debt / GDP", fmt(v, "%"), d is not None and f"{d:+.1f} pp vs 1 year ago",
    sig["debt"], t is not None and f"Latest: {t:%b %Y}")
v, d, t = last_delta(credit_total_yoy, 1)
kpi(r2[2], "Private sector · Credit YoY", fmt(v, "%"), d is not None and f"{d:+.1f} pp vs prior month",
    sig["credit"], t is not None and f"Latest: {t:%b %Y} · nominal")
if revenue is not None:
    kpi(r2[3], "Taxes · Federal revenue (12m)", f"R$ {revenue['R$ billion'].sum() / 1000:,.2f} tn",
        rev_extra.get("transfers") and f"R$ {rev_extra['transfers']:,.0f} bn sent to states & cities",
        ("neutral", "Structural"), f"Through bimester {rreo3[1]}/{rreo3[0]}")
else:
    kpi(r2[3], "Taxes · Federal revenue (12m)", "n/a", None, None, None)

st.write("")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_macro, tab_private, tab_taxes = st.tabs(["📈  Macro", "🏢  Private sector", "🏛️  Taxes & spending"])

with tab_macro:
    st.write("")
    c1, c2 = st.columns(2, gap="large")
    if ibc_yoy is not None:
        card_head(c1, "Economic activity (IBC-Br), YoY %", "ibc", sig["ibc"])
        show(c1, line_chart({"IBC-Br YoY": ibc_yoy}, "%", start, zero=True, fmt_last="{:.1f}%"))
        card_foot(c1, "ibc", sig["ibc"])
    if sgs["ipca_12m"] is not None:
        card_head(c2, "Inflation (IPCA, 12-month %)", "ipca", sig["ipca"])
        show(c2, line_chart({"IPCA 12m": sgs["ipca_12m"]}, "%", start,
                            band=(IPCA_TARGET - IPCA_TOL, IPCA_TARGET + IPCA_TOL),
                            hline=(IPCA_TARGET, f"Target {IPCA_TARGET}%"), fmt_last="{:.2f}%"))
        card_foot(c2, "ipca", sig["ipca"])

    c3, c4 = st.columns(2, gap="large")
    if selic_m is not None:
        card_head(c3, "Selic policy rate (% p.a.)", "selic", sig["selic"])
        show(c3, line_chart({"Selic": selic_m}, "%", start, fmt_last="{:.2f}%"))
        card_foot(c3, "selic", sig["selic"])
    if ptax_m is not None:
        card_head(c4, "USD/BRL exchange rate (month-end)", "fx", sig["fx"])
        show(c4, line_chart({"USD/BRL": ptax_m}, "R$ per US$", start, fmt_last="{:.2f}"))
        card_foot(c4, "fx", sig["fx"])

    c5, c6 = st.columns(2, gap="large")
    if unemp is not None:
        card_head(c5, "Unemployment rate (%)", "unemp", sig["unemp"])
        show(c5, line_chart({"Unemployment": unemp}, "%", start, fmt_last="{:.1f}%"))
        card_foot(c5, "unemp", sig["unemp"])
    if sgs["dbgg"] is not None:
        card_head(c6, "Gross public debt (% of GDP)", "debt", sig["debt"])
        show(c6, line_chart({"Gross debt": sgs["dbgg"]}, "% of GDP", start, fmt_last="{:.1f}%"))
        card_foot(c6, "debt", sig["debt"])

    with st.expander("📋 Data table & download"):
        table = pd.DataFrame({
            "IBC-Br YoY %": ibc_yoy, "IPCA 12m %": sgs["ipca_12m"], "Selic %": selic_m,
            "USD/BRL": ptax_m, "Unemployment %": unemp, "Gross debt % GDP": sgs["dbgg"],
        })
        table = table[table.index >= start].sort_index(ascending=False)
        st.dataframe(table.style.format("{:.2f}", na_rep=""), width="stretch")
        st.download_button("Download CSV", table.to_csv().encode(), "brazil_macro.csv", "text/csv")

with tab_private:
    st.write("")
    c1, c2 = st.columns(2, gap="large")
    if credit_pj_yoy is not None and credit_pf_yoy is not None:
        card_head(c1, "Credit outstanding, YoY % (nominal)", "credit", sig["credit"])
        series = {"Companies": credit_pj_yoy, "Households": credit_pf_yoy}
        fig = line_chart(series, "%", start, zero=True, fmt_last="{:.1f}%")
        if sgs["ipca_12m"] is not None:
            ip = sgs["ipca_12m"][sgs["ipca_12m"].index >= start]
            fig.add_trace(go.Scatter(x=ip.index, y=ip.values, name="IPCA 12m (inflation)", mode="lines",
                                     line=dict(color=INK_3, width=1.5, dash="dot"),
                                     hovertemplate="IPCA 12m: %{y:.2f}<extra></extra>"))
        show(c1, fig)
        card_foot(c1, "credit", sig["credit"])
    if npl_m is not None:
        card_head(c2, "Non-performing loans, 90+ days (% of loans)", "npl", sig["npl"])
        show(c2, line_chart({"Default rate": npl_m}, "%", start, fmt_last="{:.2f}%"))
        card_foot(c2, "npl", sig["npl"])

with tab_taxes:
    st.write("")
    if rreo3 is not None and revenue is not None:
        card_head(st, f"Where the money comes from: federal current revenue, last 12 months "
                      f"(through bimester {rreo3[1]}/{rreo3[0]})", "revenue")
        show(st, bar_chart(revenue, "Source"))
        extra = ""
        if rev_extra.get("transfers") is not None:
            share = rev_extra["transfers"] / revenue["R$ billion"].sum()
            extra = (f"<b>Now:</b> R$ {rev_extra['transfers']:,.0f} bn ({share:.0%} of the total) went to "
                     f"states and municipalities. Net current revenue (RCL): "
                     f"R$ {fmt(rev_extra.get('rcl'), '', 0)} bn.<br>")
        st.markdown(f'<div class="read">{extra}<b>How to read it:</b> {EXPLAIN["revenue"]["how"]}</div>',
                    unsafe_allow_html=True)

    if rreo2 is not None and spending is not None:
        card_head(st, f"Where the money goes: federal spending by function, "
                      f"{rreo2[0]} year-to-date (through bimester {rreo2[1]})", "spending")
        exclude = st.checkbox("Hide 'Special Charges' (debt service, refinancing, transfers)", value=True)
        enc_label = FUNCTIONS["Encargos Especiais"]
        shown = spending[spending["Function"] != enc_label] if exclude else spending
        show(st, bar_chart(shown, "Function"))
        enc = spending.loc[spending["Function"] == enc_label, "R$ billion"]
        top3 = shown.head(3)
        top_share = top3["R$ billion"].sum() / shown["R$ billion"].sum()
        now = (f"<b>Now:</b> The top 3 functions ({', '.join(top3['Function'])}) account for "
               f"{top_share:.0%} of the spending shown."
               + (f" Special Charges: R$ {enc.iloc[0]:,.0f} bn." if not enc.empty else "") + "<br>")
        st.markdown(f'<div class="read">{now}<b>How to read it:</b> {EXPLAIN["spending"]["how"]} '
                    f'Measure: {spend_col}.</div>', unsafe_allow_html=True)

st.caption("Built with public data from Banco Central do Brasil, IBGE and Tesouro Nacional. "
           "Status tags are simple rule-based reads, not forecasts.")
