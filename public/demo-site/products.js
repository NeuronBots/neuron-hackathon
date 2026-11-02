/* products.js — one job: the editable candle list. Owner edits prices/stock here. */
var PRODUCTS = [
  { id: "vanilla-ember", name: "Vanilla Ember", scent_notes: "Vanilla bean, smoked sugar, a breath of clove", price_inr: 499, image: "images/vanilla-ember.webp", status: "in_stock" },
  { id: "monsoon-jasmine", name: "Monsoon Jasmine", scent_notes: "Night jasmine, wet earth, white tea", price_inr: 549, image: "images/monsoon-jasmine.webp", status: "in_stock" },
  { id: "filter-kaapi", name: "Filter Kaapi", scent_notes: "Roasted coffee, jaggery, green cardamom", price_inr: 449, image: "images/filter-kaapi.webp", status: "in_stock" },
  { id: "sandalwood-dusk", name: "Sandalwood Dusk", scent_notes: "Mysore sandalwood, amber, soft musk", price_inr: 649, image: "images/sandalwood-dusk.webp", status: "in_stock" },
  { id: "gulab-oud", name: "Gulab Oud", scent_notes: "Damask rose, oud, a thread of saffron", price_inr: 749, image: "images/gulab-oud.webp", status: "out_of_stock" }
];
if (typeof module !== "undefined") module.exports = { PRODUCTS: PRODUCTS };
