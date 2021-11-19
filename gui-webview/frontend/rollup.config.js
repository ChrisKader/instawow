import commonjs from "@rollup/plugin-commonjs";
import json from "@rollup/plugin-json";
import html from '@rollup/plugin-html';
import resolve from "@rollup/plugin-node-resolve";
import svelte from "rollup-plugin-svelte";
import typescript from "@rollup/plugin-typescript";
import css from "rollup-plugin-css-only";
import sveltePreprocess from "svelte-preprocess";

const production = !process.env.ROLLUP_WATCH;

export default [
  {
    input: "src/index.ts",
    output: {
      dir: "../../.dist",
      name: 'index',
      sourcemap: !production,
      format: "iife",
      //file: "svelte-bundle.js",
      exports: "auto",
    },
    plugins: [
      svelte({
        preprocess: sveltePreprocess({ sourceMap: !production }),
        compilerOptions: {
          // enable run-time checks when not in production
          dev: !production,
        },
      }),
      // we'll extract any component CSS out into
      // a separate file - better for performance
      css({ output: "stylesheet.css" }),
      // If you have external dependencies installed from
      // npm, you'll most likely need these plugins. In
      // some cases you'll need additional configuration -
      // consult the documentation for details:
      // https://github.com/rollup/plugins/tree/master/packages/commonjs
      resolve({
        browser: true,
        dedupe: ["svelte"],
        preferBuiltins: false,
      }),
      typescript({
        sourceMap: !production,
        inlineSources: !production,
      }),
      html({
        template: () => { 
          return `<!DOCTYPE html\n>`+
                 `<html lang="en">\n` +
                 `  <head>\n` +
                 `    <meta charset="utf-8" />\n` +
                 `    <!-- https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy/script-src -->\n` +
                 `    <meta http-equiv="Content-Security-Policy" content="script-src 'self';" />\n` +
                 `    <link rel="stylesheet" href="stylesheet.css" />\n` +
                 `    <script defer src="index.js"></script>\n` +
                 `  </head>\n` +
                 `  <body>\n` +
                 `    <!--  -->\n` +
                 `  </body>\n` +
                 `</html>`
      }
      }),
      commonjs(),
      json(),
    ],
    watch: {
      clearScreen: false,
    },
  },
];
