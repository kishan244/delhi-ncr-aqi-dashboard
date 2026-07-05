import matplotlib
matplotlib.use('Agg')

from flask import Flask, render_template, request
import seaborn as sns
import matplotlib.pyplot as plt
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import matplotlib.colors as mcolors
import io
import base64
import json
import os
from urllib.parse import urlencode
from urllib.request import urlopen

app = Flask(__name__)

def plot_to_base64():
    img = io.BytesIO()
    plt.savefig(img, format='png', bbox_inches="tight")
    img.seek(0)
    plot_url = base64.b64encode(img.getvalue()).decode()
    plt.close()
    return plot_url


def add_bounds_padding(bounds, ratio=0.04):
    minx, miny, maxx, maxy = bounds
    x_pad = (maxx - minx) * ratio
    y_pad = (maxy - miny) * ratio
    return minx - x_pad, miny - y_pad, maxx + x_pad, maxy + y_pad


def fetch_json(url, timeout=10):
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def fetch_live_delhi_aqi(token):
    query = urlencode({
        "token": token,
        "keyword": "Delhi"
    })
    url = f"https://api.waqi.info/search/?{query}"
    payload = fetch_json(url)

    if payload.get("status") != "ok":
        return []

    stations = []
    for item in payload.get("data", []):
        station = item.get("station", {})
        geo = station.get("geo") or []
        aqi_value = item.get("aqi")

        if not isinstance(geo, list) or len(geo) != 2:
            continue

        try:
            latitude = float(geo[0])
            longitude = float(geo[1])
        except (TypeError, ValueError):
            continue

        try:
            aqi_numeric = float(aqi_value)
        except (TypeError, ValueError):
            continue

        stations.append({
            "station": station.get("name", "Unknown"),
            "latitude": latitude,
            "longitude": longitude,
            "aqi": aqi_numeric,
        })

    return stations


@app.route("/")
def dashboard():
    df = pd.read_csv("delhi_ncr_aqi_dataset.csv")
    df['datetime'] = pd.to_datetime(df['datetime'])

    years = sorted(df["year"].dropna().astype(int).unique().tolist())
    stations = sorted(df["station"].dropna().astype(str).unique().tolist())

    selected_year = request.args.get("year", "all")
    selected_station = request.args.get("station", "all")
    waqi_token = os.getenv("WAQI_TOKEN", "demo")
    live_map_status = "Fallback CSV"

    df_filtered = df
    if selected_year != "all":
        try:
            year_int = int(selected_year)
            df_filtered = df_filtered[df_filtered["year"] == year_int]
        except ValueError:
            selected_year = "all"

    if selected_station != "all":
        df_filtered = df_filtered[df_filtered["station"].astype(str) == selected_station]

    sns.set_theme(style="whitegrid")
    plt.rcParams['figure.figsize'] = (10,6)
    plt.rcParams['axes.titleweight'] = 'bold'
    plt.rcParams['axes.titlesize'] = 14
    plt.rcParams['axes.labelsize'] = 12

    plots = {}

    plt.figure()

    # 1. Distribution of AQI Levels by Category
    sns.histplot(
        data=df_filtered,
        x="aqi",
        hue="aqi_category",
        palette="viridis",
        bins=40,
        kde=True,
        multiple="stack"
    )
    plt.title("1. Distribution of AQI Levels by Category")
    plt.xlabel("AQI Value")
    plt.ylabel("Frequency")
    plt.tight_layout()
    plots["plot1"] = plot_to_base64()

    plt.figure()

    # 2. AQI Distribution Across Seasons
    sns.boxplot(
        data=df_filtered,
        x="season",
        y="aqi",
        palette="coolwarm"
    )
    plt.title("2. AQI Distribution Across Seasons")
    plt.xlabel("Season")
    plt.ylabel("AQI")
    plt.xticks(rotation=20)
    plt.tight_layout()
    plots["plot2"] = plot_to_base64()

    df_filtered['hour'] = df_filtered['datetime'].dt.hour
    hourly_avg = df_filtered.groupby('hour')['aqi'].mean().reset_index()

    plt.figure()

    
    kpi = {
        "total_records": len(df_filtered),
        "avg_aqi": df_filtered["aqi"].mean() if len(df_filtered) else 0,
        "max_aqi": int(df_filtered["aqi"].max()) if len(df_filtered) else 0,
        "num_stations": df_filtered["station"].nunique(),
    }

    # 18. Delhi district AQI map with station points
    delhi_map = None
    district_map = None
    try:
        districts = gpd.read_file("maps-master/Districts/Census_2011/2011_Dist.shp")
        delhi_districts = districts[districts["ST_NM"] == "NCT of Delhi"]

        station_aqi = (
            df_filtered
            .groupby(["station", "latitude", "longitude"])["aqi"]
            .mean()
            .reset_index()
        )

        aqi_gdf = gpd.GeoDataFrame(
            station_aqi,
            geometry=[Point(xy) for xy in zip(
                station_aqi["longitude"],
                station_aqi["latitude"]
            )],
            crs="EPSG:4326"
        )

        delhi_districts = delhi_districts.to_crs(epsg=3857)
        base_districts = delhi_districts.copy()
        aqi_gdf = aqi_gdf.to_crs(epsg=3857)

        joined = gpd.sjoin(
            aqi_gdf,
            delhi_districts,
            how="inner",
            predicate="within"
        )

        district_aqi = (
            joined
            .groupby("DISTRICT")["aqi"]
            .mean()
            .reset_index()
        )

        district_view = base_districts.merge(
            district_aqi,
            on="DISTRICT",
            how="left"
        )

        min_aqi = district_view["aqi"].min()
        max_aqi = district_view["aqi"].max()

        cmap = plt.cm.Reds
        norm = mcolors.Normalize(vmin=min_aqi, vmax=max_aqi)

        fig, ax = plt.subplots(figsize=(8.8, 6.4))
        district_view.plot(
            ax=ax,
            column="aqi",
            cmap=cmap,
            norm=norm,
            legend=True,
            edgecolor="#1f2937",
            linewidth=1,
            missing_kwds={"color": "lightgrey"}
        )
        minx, miny, maxx, maxy = add_bounds_padding(base_districts.total_bounds)
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.set_axis_off()
        ax.set_title("Delhi District-wise AQI", pad=10)
        plt.tight_layout(pad=0.5)
        district_map = plot_to_base64()

        live_station_rows = fetch_live_delhi_aqi(waqi_token)
        if live_station_rows:
            live_map_status = "Live API"
            if waqi_token == "demo":
                live_map_status = "Live API (Demo Token)"
            live_station_df = pd.DataFrame(live_station_rows)
            live_gdf = gpd.GeoDataFrame(
                live_station_df,
                geometry=[Point(xy) for xy in zip(
                    live_station_df["longitude"],
                    live_station_df["latitude"]
                )],
                crs="EPSG:4326"
            ).to_crs(epsg=3857)

            live_joined = gpd.sjoin(
                live_gdf,
                base_districts,
                how="inner",
                predicate="within"
            )

            live_district_aqi = (
                live_joined
                .groupby("DISTRICT")["aqi"]
                .max()
                .reset_index()
            )

            live_view = base_districts.merge(
                live_district_aqi,
                on="DISTRICT",
                how="left"
            )
        else:
            live_joined = gpd.sjoin(
                aqi_gdf,
                base_districts,
                how="inner",
                predicate="within"
            )

            live_district_aqi = (
                live_joined
                .groupby("DISTRICT")["aqi"]
                .max()
                .reset_index()
            )

            live_view = base_districts.merge(
                live_district_aqi,
                on="DISTRICT",
                how="left"
            )

        fig, ax = plt.subplots(figsize=(8.8, 6.4))
        live_view.plot(
            ax=ax,
            column="aqi",
            cmap="Blues",
            legend=True,
            edgecolor="#1f2937",
            linewidth=1,
            missing_kwds={"color": "#e5e7eb"}
        )
        for _, row in live_view.dropna(subset=["aqi"]).iterrows():
            point = row.geometry.representative_point()
            ax.text(
                point.x,
                point.y,
                f"{int(round(row['aqi']))}",
                ha="center",
                va="center",
                fontsize=9,
                fontweight="bold",
                color="#0f172a",
                bbox={
                    "boxstyle": "round,pad=0.22",
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.82,
                },
            )
        minx, miny, maxx, maxy = add_bounds_padding(base_districts.total_bounds)
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.set_axis_off()
        ax.set_title("Delhi Live Station View", pad=10)
        plt.tight_layout(pad=0.5)
        delhi_map = plot_to_base64()

    except Exception:
        pass

    return render_template(
        "index.html",
        plots=plots,
        kpi=kpi,
        delhi_map=delhi_map,
        district_map=district_map,
        live_map_status=live_map_status,
        years=years,
        stations=stations,
        selected_year=str(selected_year),
        selected_station=str(selected_station),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
