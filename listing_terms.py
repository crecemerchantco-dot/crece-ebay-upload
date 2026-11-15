"""Read the selected policies before showing the seller a publication action."""
import hashlib
import json
import re
from ebay_checks import EbayChecks, CheckError
from ebay_publish import PublishError


def selected_policies(token, item, http=None):
    terms = item['listingTerms']
    shipping = terms['fulfillmentTerms']['shippingTerms']
    # A single saved policy avoids concealed per-listing shipping overrides.
    if set(shipping) != {'businessPolicy'}:
        raise PublishError('Prepare shipping using one saved policy, without additional shipping overrides.')
    ids = {'fulfillment': shipping['businessPolicy']['businessPolicyId'],
           'return': terms['returnTerms']['businessPolicyId'],
           'payment': terms['paymentTerms']['businessPolicyId']}
    client = EbayChecks(token, http=http)
    snapshots = {}
    try:
        for kind, value in ids.items():
            if not re.fullmatch(r'[0-9]+', str(value)):
                raise PublishError('Choose a saved eBay selling policy.')
            snapshots[kind] = client._get('/sell/account/v1/' + kind + '_policy/' + str(value))
    except CheckError:
        raise PublishError('The selected selling policies could not be read. Reconnect eBay before reviewing price and shipping.') from None
    lines = []
    fulfillment = snapshots['fulfillment']
    for option in fulfillment.get('shippingOptions', []):
        scope = 'International' if option.get('optionType') == 'INTERNATIONAL' else 'Domestic'
        for service in option.get('shippingServices', []):
            name = service.get('shippingServiceCode', 'Shipping service')
            if service.get('freeShipping'):
                rate = 'free shipping'
            elif option.get('costType') == 'CALCULATED':
                rate = 'calculated shipping paid by buyer'
            elif option.get('costType') == 'FLAT_RATE' and service.get('shippingCost'):
                cost = service['shippingCost']
                rate = str(cost.get('value', '')) + ' ' + str(cost.get('currency', '')) + ' shipping'
            else:
                raise PublishError('Shipping price could not be explained from the selected policy. Review that policy before publishing.')
            lines.append(scope + ': ' + name + ' — ' + rate)
    if not lines:
        if fulfillment.get('localPickup'):
            lines = ['Local pickup']
        else:
            raise PublishError('The selected policy did not return a shipping service. Review it before publishing.')
    handling = fulfillment.get('handlingTime', {})
    if handling.get('value') is not None:
        lines.append('Handling: ' + str(handling['value']) + ' ' + str(handling.get('unit', '')).lower())
    returns = snapshots['return']
    if returns.get('returnsAccepted'):
        duration = returns.get('returnPeriod', {})
        return_text = str(duration.get('value', '')) + ' ' + str(duration.get('unit', '')).lower() + ' returns; return shipping paid by ' + str(returns.get('returnShippingCostPayer', 'see policy')).lower()
    else:
        return_text = 'Returns not accepted under the seller return policy'
    # Fingerprint all fields: even a changed field not summarized above requires
    # another review rather than silently approving a policy change.
    fingerprint = hashlib.sha256(json.dumps(snapshots, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'shipping': lines, 'returns': return_text, 'fingerprint': fingerprint,
            'names': {k: v.get('name', '') for k, v in snapshots.items()}}
