import {readFile, writeFile} from 'node:fs/promises';
import {getMapData, getVersion} from '@mappedin/mappedin-js';

export function parseEnv(text) {
  const result = {};
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const match = line.match(/^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!match) throw new Error('Invalid credentials file syntax');
    let value = match[2];
    if (/^['"]/.test(value)) {
      if (value.at(-1) !== value[0]) throw new Error('Invalid quoted credential');
      value = value.slice(1, -1);
    }
    result[match[1]] = value;
  }
  for (const name of ['MAPPEDIN_KEY_ID', 'MAPPEDIN_KEY', 'MAPPEDIN_VENUE']) {
    if (!result[name]) throw new Error('Missing Mappedin configuration');
  }
  return result;
}

export function sanitize(value, secrets) {
  if (typeof value === 'string') {
    if (secrets.some(secret => secret && value.includes(secret))) return null;
    return value;
  }
  if (Array.isArray(value)) return value.map(item => sanitize(item, secrets));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value)
      .filter(([key]) => !/^(key|secret|accessToken|refreshToken|apiKey|clientSecret|authorization|outdoorViewToken)$/i.test(key))
      .map(([key, item]) => [key, sanitize(item, secrets)]));
  }
  return value;
}

export async function distances(map, targets, progress = () => {}) {
  const rows = [];
  for (const from of targets) {
    for (const to of targets) {
      if (from.id === to.id) continue;
      const route = await map.getDirections(from, to);
      if (route && (!Number.isFinite(route.distance) || route.distance < 0)) {
        throw new Error('Invalid route distance');
      }
      rows.push({from_id: from.id, to_id: to.id,
        distance_m: route ? route.distance : null,
        status: route ? 'reachable' : 'unreachable'});
    }
    progress(rows.length);
  }
  return rows;
}

const collections = {
  locations: 'enterprise-location', categories: 'enterprise-category', venues: 'enterprise-venue',
  floors: 'floor', buildings: 'floor-stack', spaces: 'space', connections: 'connection',
  points_of_interest: 'point-of-interest', doors: 'door', objects: 'object', areas: 'area',
  annotations: 'annotation', facades: 'facade', location_profiles: 'location-profile',
  location_categories: 'location-category',
};
const referenceFields = ['floor', 'floors', 'spaces', 'categories', 'children', 'locations',
  'points', 'objects', 'instances', 'space', 'object'];
const entityKey = object => `${object.__type}:${object.id}`;
const plain = (value, seen = new WeakSet()) => {
  if (value == null || typeof value !== 'object') return value;
  if (value.__type && value.id) return {id: value.id, type: value.__type};
  if (seen.has(value)) return undefined;
  seen.add(value);
  if (Array.isArray(value)) return value.map(item => plain(item, seen));
  return Object.fromEntries(Object.entries(value).filter(([key]) => !key.startsWith('_'))
    .map(([key, item]) => [key, plain(item, seen)]));
};

async function exportMap(envFile, output) {
  const env = parseEnv(await readFile(envFile, 'utf8'));
  const secrets = [env.MAPPEDIN_KEY_ID, env.MAPPEDIN_KEY];
  const map = await getMapData({key: env.MAPPEDIN_KEY_ID, secret: env.MAPPEDIN_KEY,
    mapId: env.MAPPEDIN_VENUE});
  if (!map.isEnterpriseMode) throw new Error('Expected an enterprise venue');
  const bundle = sanitize(await map.toJSONBundle({downloadLanguagePacks: true}), secrets);
  await writeFile(`${output}/map.json`, JSON.stringify(bundle));
  const entities = new Map();
  const groups = {};
  const relations = new Map();
  const navigable = new Set();
  const add = (object, group) => {
    if (!object?.id || !object.__type) throw new Error('Missing source entity identity');
    const key = entityKey(object);
    if (!entities.has(key)) entities.set(key, {object, group});
    return key;
  };
  for (const [group, type] of Object.entries(collections)) {
    const found = map.getByType(type);
    groups[group] = (Array.isArray(found) ? found : found ? [found] : []).map(o => add(o, group));
  }
  groups.nodes = (await map.Nodes.getAll()).map(o => add(o, 'nodes'));
  const link = (source, target, relation) => {
    const src = entityKey(source);
    const dst = add(target, Object.keys(collections).find(k => collections[k] === target.__type) ?? 'nodes');
    relations.set(JSON.stringify([src, relation, dst]), {src, dst, relation});
  };
  for (const {object} of entities.values()) {
    for (const field of referenceFields) {
      const value = object[field];
      for (const target of Array.isArray(value) ? value : value ? [value] : []) {
        if (target?.id && target.__type) link(object, target, field);
      }
    }
    if (object.__type === 'enterprise-location' || object.__type === 'connection') {
      for (const node of await map.Nodes.getByType(object)) {
        link(object, node, 'nodes');
        if (object.__type === 'enterprise-location') navigable.add(object.id);
      }
    }
    if (object.__type === 'node') {
      for (const neighbor of await object.getNeighbors()) link(object, neighbor, 'neighbor');
    }
  }
  const records = [];
  for (const [key, {object, group}] of entities) {
    const properties = {};
    for (const [field, value] of Object.entries(object)) {
      if (!field.startsWith('_')) properties[field] = plain(value);
    }
    for (const field of ['name', 'externalId', 'type', 'hidden', 'amenity', 'description',
      'elevation', 'coordinate', 'coordinates', 'navigationFlags', 'center']) {
      if (field in object) properties[field] = plain(object[field]);
    }
    records.push({entity_id: key, id: object.id, type: object.__type, collection: group,
      name: object.name ?? null, external_id: object.externalId ?? null,
      description: object.description ?? null, floor_id: object.floor?.id ?? null,
      hidden: object.hidden ?? null, navigable: object.__type === 'enterprise-location'
        ? navigable.has(object.id) : null, amenity: object.amenity ?? null,
      elevation: object.elevation ?? null,
      properties_json: JSON.stringify(sanitize(properties, secrets)),
      geometry_json: object.geoJSON ? JSON.stringify(sanitize(object.geoJSON, secrets)) : null});
  }
  const targets = [...entities.values()].map(e => e.object)
    .filter(o => o.__type === 'enterprise-location' && navigable.has(o.id))
    .sort((a, b) => a.id.localeCompare(b.id));
  console.error(`Loaded ${records.length} entities; routing ${targets.length} locations (${targets.length * (targets.length - 1)} ordered pairs)`);
  const routes = await distances(map, targets, completed => {
    console.error(`Walking distances: ${completed}/${targets.length * (targets.length - 1)}`);
  });
  const snapshot = sanitize({format_version: 1, collected_at: new Date().toISOString(),
    venue_id: map.mapId, venue_name: map.mapName, map_updated_at: map.updatedAt,
    sdk_version: getVersion(), collections: [...Object.keys(collections), 'nodes'],
    brand_identity: 'No distinct brand entity is exposed by the enterprise SDK; retailer metadata is in locations.',
    routing: {profile: 'standard', options: {}, units: 'metres', directed: true,
      include_hidden: true, include_self: false, target_ids: targets.map(t => t.id)},
    entities: records, relationships: [...relations.values()], distances: routes}, secrets);
  await writeFile(`${output}/snapshot.json`, JSON.stringify(snapshot));
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1], 'file:').href) {
  try {
    await exportMap(process.argv[2], process.argv[3]);
    process.exit(0);
  } catch {
    console.error('Mappedin export failed; no dataset was published.');
    process.exit(1);
  }
}
