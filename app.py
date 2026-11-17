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

# Palette (validated categorical slots 1-2 + neutral reference ink)
BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8984"

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
# Chart helpers
# ---------------------------------------------------------------------------
def line_chart(series: dict, title: str, ytitle: str, start, band=None, hline=None,
               zero=False, height=320):
    fig = go.Figure()
    if band is not None:
        fig.add_hrect(y0=band[0], y1=band[1], fillcolor=GRAY, opacity=0.12, line_width=0,
                      annotation_text="Tolerance band", annotation_position="top left",
                      annotation_font_color=GRAY)
    if hline is not None:
        fig.add_hline(y=hline[0], line_dash="dash", line_color=GRAY, line_width=1,
                      annotation_text=hline[1], annotation_position="bottom left",
                      annotation_font_color=GRAY)
    if zero:
        fig.add_hline(y=0, line_color=GRAY, line_width=1)
    colors = [BLUE, ORANGE]
    for i, (name, s) in enumerate(series.items()):
        s = s[s.index >= start]
        fig.add_trace(go.Scatter(x=s.index, y=s.values, name=name, mode="lines",
                                 line=dict(color=colors[i % 2], width=2)))
    fig.update_layout(
        title=dict(text=title, font_size=15), height=height,
        margin=dict(l=10, r=10, t=50, b=10), hovermode="x unified",
        yaxis_title=ytitle, showlegend=len(series) > 1,
        legend=dict(orientation="h", y=1.02, x=1, xanchor="right", yanchor="bottom"),
    )
    fig.update_xaxes(showgrid=False)
    return fig


def bar_chart(df: pd.DataFrame, label: str, title: str, height=None):
    df = df.sort_values("R$ billion")
    fig = go.Figure(go.Bar(
        x=df["R$ billion"], y=df[label], orientation="h", marker_color=BLUE,
        text=df["R$ billion"].map(lambda v: f"{v:,.0f}"), textposition="outside",
        hovertemplate="%{y}: R$ %{x:,.1f} bn<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, font_size=15), height=height or max(300, 28 * len(df) + 80),
        margin=dict(l=10, r=40, t=50, b=10), xaxis_title="R$ billion",
    )
    fig.update_xaxes(showgrid=False)
    return fig


def safe(label: str, fn, *args):
    try:
        return fn(*args)
    except Exception as err:  # noqa: BLE001
        st.warning(f"Could not load **{label}** right now ({err}). Try the refresh button later.")
        return None


def fmt(v, suffix="", decimals=1):
    return "n/a" if v is None or pd.isna(v) else f"{v:,.{decimals}f}{suffix}"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    years = st.slider("History shown (years)", 1, 9, 5)
    if st.button("🔄 Refresh data now"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Data is cached and refreshed automatically every 7 days.")
    st.divider()
    st.caption(
        "**Sources:** Banco Central do Brasil (SGS, Focus), IBGE (SIDRA / PNAD Contínua), "
        "Tesouro Nacional (Siconfi – RREO)."
    )

start = pd.Timestamp(dt.date.today()) - pd.DateOffset(years=years)

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
st.title("🇧🇷 Brazil Economy Monitor")
st.caption(f"Page loaded {dt.datetime.now():%b %d, %Y %H:%M} · data cached weekly")

with st.spinner("Loading data from BCB, IBGE and Tesouro..."):
    sgs = {k: safe(f"SGS {code}", fetch_sgs, code) for k, code in SGS.items()}
    unemp = safe("Unemployment (IBGE)", fetch_unemployment)
    focus = safe("Focus survey (BCB)", fetch_focus_ipca)
    rreo2 = safe("Federal spending (Tesouro)", latest_rreo, "RREO-Anexo 02")
    rreo3 = safe("Federal revenue (Tesouro)", latest_rreo, "RREO-Anexo 03")

ibc_yoy = yoy(sgs["ibcbr"]) if sgs["ibcbr"] is not None else None
selic_m = monthly_last(sgs["selic"]) if sgs["selic"] is not None else None
ptax_m = monthly_last(sgs["ptax"]) if sgs["ptax"] is not None else None
credit_pj_yoy = yoy(sgs["credit_pj"]) if sgs["credit_pj"] is not None else None
credit_pf_yoy = yoy(sgs["credit_pf"]) if sgs["credit_pf"] is not None else None
credit_total_yoy = (
    yoy(sgs["credit_pj"].add(sgs["credit_pf"]).dropna())
    if sgs["credit_pj"] is not None and sgs["credit_pf"] is not None else None
)

this_year = str(dt.date.today().year)
focus_now = None
if focus is not None:
    row = focus[focus["DataReferencia"] == this_year]
    focus_now = float(row["Mediana"].iloc[0]) if not row.empty else None

revenue, rev_extra, spending, spend_col = None, {}, None, None
if rreo3 is not None:
    res = safe("Federal revenue breakdown", revenue_by_type, rreo3[2])
    if res:
        revenue, rev_extra = res
if rreo2 is not None:
    res = safe("Federal spending breakdown", spending_by_function, rreo2[2])
    if res:
        spend_col, spending = res

# ---------------------------------------------------------------------------
# KPI row (8 indicators)
# ---------------------------------------------------------------------------
def last_and_delta(s, lag_months=1):
    if s is None or len(s) <= lag_months:
        return None, None, None
    return float(s.iloc[-1]), float(s.iloc[-1] - s.iloc[-1 - lag_months]), s.index[-1]


st.subheader("Snapshot")
r1 = st.columns(4)
r2 = st.columns(4)

v, d, t = last_and_delta(ibc_yoy)
r1[0].metric("1 · Activity (IBC-Br, YoY)", fmt(v, "%"), None if d is None else f"{d:+.1f} pp vs prior month",
             help=f"Monthly GDP proxy. Latest: {t:%b %Y}" if t is not None else None)

v, d, t = last_and_delta(sgs["ipca_12m"])
r1[1].metric("2 · Inflation (IPCA, 12m)", fmt(v, "%"),
             None if d is None else f"{d:+.2f} pp vs prior month", delta_color="inverse",
             help=(f"Target {IPCA_TARGET}% ± {IPCA_TOL} pp. "
                   f"Focus median for {this_year}: {fmt(focus_now, '%', 2)}. "
                   + (f"Latest: {t:%b %Y}" if t is not None else "")))

if sgs["selic"] is not None:
    s = sgs["selic"]
    r1[2].metric("3 · Selic rate", fmt(s.iloc[-1], "%", 2),
                 f"{s.iloc[-1] - value_ago(s, 365):+.2f} pp vs 1 year ago", delta_color="off",
                 help=f"As of {s.index[-1]:%b %d, %Y}")
else:
    r1[2].metric("3 · Selic rate", "n/a")

if sgs["ptax"] is not None:
    s = sgs["ptax"]
    chg = (s.iloc[-1] / value_ago(s, 30) - 1) * 100
    r1[3].metric("4 · USD/BRL (PTAX)", fmt(s.iloc[-1], "", 3), f"{chg:+.1f}% vs 30 days ago",
                 delta_color="inverse", help=f"As of {s.index[-1]:%b %d, %Y}. Up = BRL weaker.")
else:
    r1[3].metric("4 · USD/BRL (PTAX)", "n/a")

v, d, t = last_and_delta(unemp, 12)
r2[0].metric("5 · Unemployment", fmt(v, "%"), None if d is None else f"{d:+.1f} pp vs 1 year ago",
             delta_color="inverse",
             help=f"PNAD Contínua, rolling quarter ending {t:%b %Y}" if t is not None else None)

v, d, t = last_and_delta(sgs["dbgg"], 12)
r2[1].metric("6 · Gross debt / GDP", fmt(v, "%"), None if d is None else f"{d:+.1f} pp vs 1 year ago",
             delta_color="inverse",
             help=f"General government gross debt (DBGG). Latest: {t:%b %Y}" if t is not None else None)

v, d, t = last_and_delta(credit_total_yoy)
npl = sgs["default"].iloc[-1] if sgs["default"] is not None else None
r2[2].metric("7 · Private credit growth (YoY)", fmt(v, "%"),
             None if d is None else f"{d:+.1f} pp vs prior month", delta_color="off",
             help=(f"Credit to companies + households, nominal. Default rate (90+ days): "
                   f"{fmt(npl, '%', 2)}. " + (f"Latest: {t:%b %Y}" if t is not None else "")))

if revenue is not None:
    total_rev = revenue["R$ billion"].sum()
    r2[3].metric("8 · Federal revenue (12m)", f"R$ {total_rev / 1000:,.2f} tn",
                 help=(f"Federal current revenue, last 12 months, through bimester "
                       f"{rreo3[1]}/{rreo3[0]}. Details in the Taxes tab."))
else:
    r2[3].metric("8 · Federal revenue (12m)", "n/a")

st.divider()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_macro, tab_private, tab_taxes = st.tabs(["📈 Macro", "🏢 Private sector", "🏛️ Taxes & spending"])

with tab_macro:
    c1, c2 = st.columns(2)
    if ibc_yoy is not None:
        c1.plotly_chart(line_chart({"IBC-Br YoY": ibc_yoy}, "Economic activity (IBC-Br), YoY %",
                                   "%", start, zero=True), width="stretch")
    if sgs["ipca_12m"] is not None:
        c2.plotly_chart(line_chart({"IPCA 12m": sgs["ipca_12m"]}, "Inflation (IPCA, 12-month %)", "%",
                                   start, band=(IPCA_TARGET - IPCA_TOL, IPCA_TARGET + IPCA_TOL),
                                   hline=(IPCA_TARGET, f"Target {IPCA_TARGET}%")),
                        width="stretch")
        if focus is not None:
            exp = ", ".join(f"{r.DataReferencia}: {r.Mediana:.2f}%"
                            for r in focus.sort_values("DataReferencia").head(2).itertuples())
            c2.caption(f"Focus survey median IPCA expectation ({focus['Data'].iloc[0]}) — {exp}")

    c3, c4 = st.columns(2)
    if selic_m is not None:
        c3.plotly_chart(line_chart({"Selic": selic_m}, "Selic target rate (% p.a.)", "%", start),
                        width="stretch")
    if ptax_m is not None:
        c4.plotly_chart(line_chart({"USD/BRL": ptax_m}, "USD/BRL (PTAX, month-end)", "R$ per US$", start),
                        width="stretch")

    c5, c6 = st.columns(2)
    if unemp is not None:
        c5.plotly_chart(line_chart({"Unemployment": unemp}, "Unemployment rate (%)", "%", start),
                        width="stretch")
    if sgs["dbgg"] is not None:
        c6.plotly_chart(line_chart({"Gross debt": sgs["dbgg"]}, "General government gross debt (% of GDP)",
                                   "% of GDP", start), width="stretch")

    with st.expander("Show data table"):
        table = pd.DataFrame({
            "IBC-Br YoY %": ibc_yoy, "IPCA 12m %": sgs["ipca_12m"], "Selic %": selic_m,
            "USD/BRL": ptax_m, "Unemployment %": unemp, "Gross debt % GDP": sgs["dbgg"],
        })
        table = table[table.index >= start].sort_index(ascending=False)
        st.dataframe(table.style.format("{:.2f}", na_rep=""), width="stretch")
        st.download_button("Download CSV", table.to_csv().encode(), "brazil_macro.csv", "text/csv")

with tab_private:
    c1, c2 = st.columns(2)
    if credit_pj_yoy is not None and credit_pf_yoy is not None:
        c1.plotly_chart(line_chart({"Companies": credit_pj_yoy, "Households": credit_pf_yoy},
                                   "Credit outstanding, YoY % (nominal)", "%", start, zero=True),
                        width="stretch")
    if sgs["default"] is not None:
        c2.plotly_chart(line_chart({"Default rate": monthly_last(sgs["default"])},
                                   "Non-performing loans, 90+ days (% of portfolio)", "%", start),
                        width="stretch")
    st.caption("Source: BCB SGS — credit outstanding to companies (20540) and households (20541); "
               "default rate (21082). Growth is nominal; compare with IPCA for real growth.")

with tab_taxes:
    if rreo3 is not None and revenue is not None:
        st.markdown(f"**Where the money comes from** — federal current revenue, last 12 months "
                    f"(through bimester {rreo3[1]}/{rreo3[0]})")
        st.plotly_chart(bar_chart(revenue, "Source", "Federal current revenue by type (R$ bn, 12m)"),
                        width="stretch")
        if rev_extra.get("transfers") is not None:
            st.caption(f"Of this, R$ {rev_extra['transfers']:,.0f} bn was transferred to states and "
                       f"municipalities (constitutional transfers). Net current revenue (RCL): "
                       f"R$ {fmt(rev_extra.get('rcl'), '', 0)} bn.")

    if rreo2 is not None and spending is not None:
        st.markdown(f"**Where the money goes** — federal spending by function, "
                    f"{rreo2[0]} year-to-date through bimester {rreo2[1]}")
        exclude = st.checkbox("Exclude 'Special Charges' (debt service, debt refinancing and "
                              "transfers — it dwarfs everything else)", value=True)
        enc_label = FUNCTIONS["Encargos Especiais"]
        shown = spending[spending["Function"] != enc_label] if exclude else spending
        st.plotly_chart(bar_chart(shown, "Function", "Federal spending by function (R$ bn)"),
                        width="stretch")
        enc = spending.loc[spending["Function"] == enc_label, "R$ billion"]
        st.caption(f"Measure: {spend_col}. "
                   + (f"Special Charges: R$ {enc.iloc[0]:,.0f} bn. " if not enc.empty else "")
                   + "Source: Tesouro Nacional, Siconfi — RREO Annexes 2 and 3 (federal government).")
