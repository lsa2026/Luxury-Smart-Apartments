const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/form-validation.js", "utf8");

function setup(language) {
    const handlers = {};
    vm.runInNewContext(source, {document: {
        documentElement: {lang: language},
        addEventListener(name, handler, capture) {
            assert.equal(capture, true);
            handlers[name] = handler;
        }
    }});
    const field = {type: "text", tagName: "INPUT", validationMessage: "native English error",
        validity: {valueMissing: true, customError: false, valid: false},
        setCustomValidity(message) {
            this.validationMessage = message;
            this.validity.customError = Boolean(message);
        }};
    return {field, fire: (name) => handlers[name]({target: field})};
}

for (const [language, required, email, checkbox] of [
    ["ar", "يرجى تعبئة هذا الحقل.", "يرجى إدخال بريد إلكتروني صحيح.", "يرجى تحديد هذا الخيار للمتابعة."],
    ["en", "Please fill out this field.", "Please enter a valid email address.", "Please check this option to continue."],
    ["fr", "Veuillez remplir ce champ.", "Veuillez saisir une adresse e-mail valide.", "Veuillez cocher cette option pour continuer."]
]) {
    test(`${language}: native required, email and checkbox validation use page language`, () => {
        const {field, fire} = setup(language);
        fire("invalid"); assert.equal(field.validationMessage, required);
        fire("invalid"); assert.equal(field.validationMessage, required);
        fire("input"); assert.equal(field.validationMessage, "");
        field.type = "email";
        field.validity = {typeMismatch: true, customError: false, valid: false};
        fire("invalid"); assert.equal(field.validationMessage, email);
        fire("change"); assert.equal(field.validationMessage, "");
        field.type = "checkbox";
        field.validity = {valueMissing: true, customError: false, valid: false};
        fire("invalid"); assert.equal(field.validationMessage, checkbox);
    });
    test(`${language}: never replace or clear custom phone/provider validation`, () => {
        const {field, fire} = setup(language);
        field.setCustomValidity("custom phone validation");
        fire("invalid"); fire("input"); fire("change");
        assert.equal(field.validationMessage, "custom phone validation");
        field.setCustomValidity(""); fire("invalid");
        field.setCustomValidity("new phone error after native check");
        fire("change"); assert.equal(field.validationMessage, "new phone error after native check");
    });
    test(`${language}: preserves numeric constraints and formats bounds locally`, () => {
        const {field, fire} = setup(language);
        field.min = "2";
        field.validity = {rangeUnderflow: true, valid: false};
        fire("invalid"); assert.ok(field.validationMessage.includes("2"));
        fire("input");
        field.validity = {valid: true};
        fire("invalid"); assert.equal(field.validationMessage, "");
    });
}
