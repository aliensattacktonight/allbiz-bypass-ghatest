"""
Per-proxy geography metadata, so the browser's timezone/locale/geolocation
match the IP it's egressing through instead of silently reporting this
machine's own settings regardless of which country the proxy sits in.

WHY THIS EXISTS
----------------
pydoll's own docs warn about exactly this: a fingerprint whose
locale/timezone contradicts the egress IP's geography is MORE suspicious
than an untouched browser (build_options()'s docstring in pydoll_test.py
already quotes this warning -- it just wasn't acted on until now). A
Chrome instance reporting Asia/Kolkata's timezone while egressing through
a Tokyo or Warsaw datacenter proxy is a free, easily-checked signal that
the traffic doesn't match its claimed origin, on top of whatever IP-level
reputation already applies.

(lat, lon) are city-centre coordinates -- accurate enough for a
geolocation override; Turnstile/Cloudflare are not expected to demand
metre-level precision, just internal consistency with the IP's country.
"""

PROXY_GEO = {
    "31.59.20.176:6754": {"country": "GB", "city": "London", "locale": "en-GB",
                           "timezone": "Europe/London", "lat": 51.5074, "lon": -0.1278},
    "31.56.127.193:7684": {"country": "US", "city": "Seattle", "locale": "en-US",
                            "timezone": "America/Los_Angeles", "lat": 47.6062, "lon": -122.3321},
    "45.38.107.97:6014": {"country": "GB", "city": "London", "locale": "en-GB",
                           "timezone": "Europe/London", "lat": 51.5074, "lon": -0.1278},
    "198.105.121.200:6462": {"country": "GB", "city": "London", "locale": "en-GB",
                             "timezone": "Europe/London", "lat": 51.5074, "lon": -0.1278},
    "64.137.96.74:6641": {"country": "ES", "city": "Madrid", "locale": "es-ES",
                           "timezone": "Europe/Madrid", "lat": 40.4168, "lon": -3.7038},
    "198.23.243.226:6361": {"country": "US", "city": "Los Angeles", "locale": "en-US",
                            "timezone": "America/Los_Angeles", "lat": 34.0522, "lon": -118.2437},
    "38.154.185.97:6370": {"country": "US", "city": "Piscataway", "locale": "en-US",
                           "timezone": "America/New_York", "lat": 40.5583, "lon": -74.4646},
    "84.247.60.125:6095": {"country": "PL", "city": "Warsaw", "locale": "pl-PL",
                           "timezone": "Europe/Warsaw", "lat": 52.2297, "lon": 21.0122},
    "142.111.67.146:5611": {"country": "JP", "city": "Tokyo", "locale": "ja-JP",
                            "timezone": "Asia/Tokyo", "lat": 35.6762, "lon": 139.6503},
    "191.96.254.138:6185": {"country": "US", "city": "Los Angeles", "locale": "en-US",
                            "timezone": "America/Los_Angeles", "lat": 34.0522, "lon": -118.2437},
}

DEFAULT_GEO = {"country": "US", "city": "Unknown", "locale": "en-US",
               "timezone": "America/New_York", "lat": 40.7128, "lon": -74.0060}


def geo_for(proxy_url):
    """Look up geo metadata for a proxy by its host:port. Falls back to a
    generic US profile for any proxy not in the table (e.g. one added to
    the pool later without an entry here yet) rather than crashing."""
    from urllib.parse import urlparse
    p = urlparse(proxy_url)
    key = f"{p.hostname}:{p.port}"
    return PROXY_GEO.get(key, DEFAULT_GEO)
