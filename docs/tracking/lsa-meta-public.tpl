___INFO___

{
  "displayName": "LSA — Meta public measurement",
  "id": "cvt_LSAMetaPublic",
  "description": "Call the first-party, consent-gated public-page Meta bridge. No identity, purchase, forms or payment data.",
  "categories": [
    "ANALYTICS"
  ],
  "type": "TAG",
  "version": 1,
  "containerContexts": [
    "WEB"
  ],
  "securityGroups": []
}

___TEMPLATE_PARAMETERS___

[]

___SANDBOXED_JS_FOR_WEB_TEMPLATE___

const callInWindow = require('callInWindow');
const copyFromDataLayer = require('copyFromDataLayer');
const eventName = copyFromDataLayer('event', 1);
const payload = {
  property_id: copyFromDataLayer('property_id', 1),
  property_name: copyFromDataLayer('property_name', 1),
  page_language: copyFromDataLayer('page_language', 1)
};
const result = callInWindow('LSAMeta.track', eventName, payload);
if (result === true) {
  data.gtmOnSuccess();
} else {
  data.gtmOnFailure();
}


___WEB_PERMISSIONS___

[
  {
    "instance": {
      "key": {
        "publicId": "access_globals",
        "versionId": "1"
      },
      "param": [
        {
          "key": "keys",
          "value": {
            "type": 2,
            "listItem": [
              {
                "type": 3,
                "mapKey": [
                  {
                    "type": 1,
                    "string": "key"
                  },
                  {
                    "type": 1,
                    "string": "read"
                  },
                  {
                    "type": 1,
                    "string": "write"
                  },
                  {
                    "type": 1,
                    "string": "execute"
                  }
                ],
                "mapValue": [
                  {
                    "type": 1,
                    "string": "LSAMeta.track"
                  },
                  {
                    "type": 8,
                    "boolean": false
                  },
                  {
                    "type": 8,
                    "boolean": false
                  },
                  {
                    "type": 8,
                    "boolean": true
                  }
                ]
              }
            ]
          }
        }
      ]
    },
    "clientAnnotations": {
      "isEditedByUser": true
    },
    "isRequired": true
  },
  {
    "instance": {
      "key": {
        "publicId": "read_data_layer",
        "versionId": "1"
      },
      "param": [
        {
          "key": "allowedKeys",
          "value": {
            "type": 1,
            "string": "specific"
          }
        },
        {
          "key": "keyPatterns",
          "value": {
            "type": 2,
            "listItem": [
              {
                "type": 1,
                "string": "event"
              },
              {
                "type": 1,
                "string": "property_id"
              },
              {
                "type": 1,
                "string": "property_name"
              },
              {
                "type": 1,
                "string": "page_language"
              }
            ]
          }
        }
      ]
    },
    "clientAnnotations": {
      "isEditedByUser": true
    },
    "isRequired": true
  }
]

___TESTS___

scenarios:
- name: Public events call only the first-party bridge
  code: |-
    mock('copyFromDataLayer', key => {
      if (key === 'event') return 'view_item';
      if (key === 'property_id') return 'synthetic-apartment';
      if (key === 'property_name') return 'Synthetic apartment';
      if (key === 'page_language') return 'en';
    });
    mock('callInWindow', (key, event, payload) => {
      assertThat(key).isEqualTo('LSAMeta.track');
      assertThat(event).isEqualTo('view_item');
      assertThat(payload).isEqualTo({property_id: 'synthetic-apartment', property_name: 'Synthetic apartment', page_language: 'en'});
      return true;
    });
    runCode({});
    assertApi('gtmOnSuccess').wasCalled();
    assertApi('gtmOnFailure').wasNotCalled();
- name: Missing website bridge does not falsely acknowledge delivery
  code: |-
    mock('copyFromDataLayer', key => 'gtm.init');
    mock('callInWindow', () => undefined);
    runCode({});
    assertApi('gtmOnFailure').wasCalled();
    assertApi('gtmOnSuccess').wasNotCalled();

___NOTES___

Private LSA container template. Only public PageView/ViewContent after a fresh Meta marketing choice. Additional consent checks: ad_storage and ad_user_data.
