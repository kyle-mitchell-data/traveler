// CloudFront Function (cloudfront-js-2.0), viewer request on /api/traveler/*.
// Sends each request to wherever the traveler currently lives, read from the
// KeyValueStore. Edge reads are free up to 2M a month; only writes cost (D5).
import cf from 'cloudfront';

const kvs = cf.kvs();

function resting(reason) {
  return {
    statusCode: 503,
    statusDescription: 'Service Unavailable',
    headers: {
      'content-type': { value: 'application/json' },
      'cache-control': { value: 'no-store' },
    },
    body: { encoding: 'text', data: JSON.stringify({ status: 'resting', reason: reason }) },
  };
}

async function handler(event) {
  let status;
  let origin;
  try {
    status = await kvs.get('status');
  } catch (e) {
    return resting('no route yet');
  }
  if (status !== 'live') {
    return resting('the traveler is resting');
  }
  try {
    origin = await kvs.get('current_origin');
  } catch (e) {
    return resting('no route yet');
  }

  // Lambda Function URLs use IAM auth; CloudFront signs the request (D17).
  cf.updateRequestOrigin({
    domainName: origin,
    originAccessControlConfig: {
      enabled: true,
      signingBehavior: 'always',
      signingProtocol: 'sigv4',
      originType: 'lambda',
    },
  });
  return event.request;
}
