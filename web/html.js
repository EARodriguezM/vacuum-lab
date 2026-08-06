// htm bound to Preact's createElement, once, for every module to import.
//
// This is what replaces JSX. `html`...`` ` `` is a tagged template, parsed in
// the browser, so there is no build step to run on the lab PC.

import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
