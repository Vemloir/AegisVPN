// Everything the site needs to know about a country, keyed by the ISO 3166-1
// alpha-2 code the API reports for a location. The database stores only that
// code: the outline and the globe's camera target come from the atlas, and the
// region is a property of the country, not of our deployment — duplicating
// either into a `servers` column would just be a second copy to keep in sync.

// world-atlas identifies features by ISO 3166-1 numeric, so we map across.
export const ISO_NUMERIC = {
  FI: '246', SE: '752', DE: '276', JP: '392', US: '840',
  NL: '528', FR: '250', GB: '826', TR: '792', PL: '616', CH: '756',
  KZ: '398', AE: '784', SG: '702', CA: '124', ES: '724', IT: '380',
}

export const REGION = {
  FI: 'eu', SE: 'eu', DE: 'eu', NL: 'eu', FR: 'eu', GB: 'eu',
  PL: 'eu', CH: 'eu', ES: 'eu', IT: 'eu', TR: 'eu',
  US: 'am', CA: 'am',
  JP: 'as', KZ: 'as', AE: 'as', SG: 'as', HK: 'as',
}

// City-states / SARs the 110m atlas has no standalone polygon for (Hong Kong is
// folded into China). The globe can't fill a country outline for these, so it
// draws a point marker at these [lon, lat] coordinates instead.
export const POINT_LOCATION = {
  HK: [114.15, 22.35],
}

// The geometric centroid of a country is not always where you want the camera:
// Alaska drags the US centroid far north-west of anywhere a user thinks of as
// "the United States". These are corrections to the atlas, not to the database.
export const CENTROID_OVERRIDE = {
  US: [-98, 39],
}

export const regionOf = (code) => REGION[code] || 'eu'
