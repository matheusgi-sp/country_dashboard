# 🇧🇷 Brazil Economy Monitor

A personal Streamlit dashboard that tracks Brazil's economy through **8 indicators**, pulled live from official, free APIs (no keys needed). Data refreshes automatically every 7 days, and you can force a refresh from the sidebar.

## The 8 indicators

| # | Pillar | Indicator | Source | Code |
|---|---|---|---|---|
| 1 | Growth | IBC-Br (monthly GDP proxy), YoY % | BCB SGS | 24363 |
| 2 | Inflation | IPCA 12-month %, vs. 3% target (±1.5 pp) and the Focus survey median | BCB SGS / Focus | 13522 |
| 3 | Monetary policy | Selic target rate | BCB SGS | 432 |
| 4 | External | USD/BRL (PTAX, selling rate) | BCB SGS | 1 |
| 5 | Labor | Unemployment rate (PNAD Contínua) | IBGE SIDRA | table 6381 |
| 6 | Fiscal | General government gross debt, % of GDP | BCB SGS | 13762 |
| 7 | Private sector | Credit to companies and households (YoY), plus default rate | BCB SGS | 20540, 20541, 21082 |
| 8 | Taxes | Federal revenue by type (last 12 months) and federal spending by function | Tesouro – Siconfi RREO | Annexes 2 and 3 |

## Layout

- **Snapshot:** 8 KPI tiles showing the latest value and its change. Hover the ⓘ icon for dates and context.
- **📈 Macro:** charts for indicators 1–6, with a data table and CSV download.
- **🏢 Private sector:** credit growth to companies vs. households, and the non-performing loan rate.
- **🏛️ Taxes & spending:** where federal money comes from and where it goes. "Special Charges" (debt service, debt refinancing and transfers) is hidden by default because it dwarfs every other function.

## Deploy (GitHub + Streamlit Community Cloud)

1. Create a new GitHub repository (it can be private) and upload `app.py`, `requirements.txt` and `README.md`.
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. Click **Create app**, select the repository and branch, and set the main file to `app.py`.
4. Click **Deploy**. The first load takes about a minute while the data is fetched.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Customizing

- **Swap a series:** edit the `SGS` dictionary at the top of `app.py`. Any series code from the [BCB SGS](https://www3.bcb.gov.br/sgspub) catalog works.
- **Refresh frequency:** change `CACHE_TTL`, which is in seconds and defaults to 7 days.
- **Inflation target:** `IPCA_TARGET` and `IPCA_TOL` are hardcoded at 3.0% ± 1.5 pp. Update them if the CMN changes the target.

## Notes and caveats

- **Release lags differ.** IBC-Br, credit and debt come out about 1.5–2 months after the reference month. Unemployment is a rolling quarter. Federal budget data (RREO) is published bimonthly, about 30 days after each bimester ends.
- **Credit growth is nominal.** Compare it with IPCA to judge real growth.
- **Revenue vs. spending periods differ.** Revenue covers the last 12 months, while spending is year-to-date "liquidated" spending.
- **If a source is down,** that section shows a warning and the rest of the dashboard still loads. The BCB API is occasionally slow or rate-limited, so try the refresh button later.
