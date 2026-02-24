// Vercel serverless function — proxies the Census Geocoder API
// to avoid CORS issues from browser requests.
// Endpoint: /api/geocode?address=275+Park+Ave+Brooklyn+NY

export default async function handler(req, res) {
  const { address } = req.query;

  if (!address) {
    return res.status(400).json({ error: 'Missing "address" query parameter' });
  }

  const url =
    'https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress' +
    `?address=${encodeURIComponent(address)}` +
    '&benchmark=Public_AR_Current&vintage=Current_Current&format=json';

  try {
    const response = await fetch(url);
    const data = await response.json();

    const match = data?.result?.addressMatches?.[0];
    if (!match) {
      return res.status(200).json({ state: null, district: null, formatted: null });
    }

    const stateAbbr = match.addressComponents?.state;
    // Census API uses session-numbered keys like "119th Congressional Districts"
    const geos = match.geographies || {};
    const cdKey = Object.keys(geos).find(k => k.toLowerCase().includes('congressional'));
    const cd = cdKey ? geos[cdKey]?.[0] : null;
    const district = cd?.BASENAME ?? cd?.CD ?? null;

    return res.status(200).json({
      state: stateAbbr,
      district: district ? String(parseInt(district)) : null,
      formatted: match.matchedAddress,
    });
  } catch (err) {
    return res.status(500).json({ error: 'Census API request failed' });
  }
}
