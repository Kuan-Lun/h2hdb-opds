#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import Ajv from "ajv";
import addFormats from "ajv-formats";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const snapshotRoot = path.resolve(
  process.env.H2HDB_OPDS_SCHEMA_ROOT ??
    path.join(repositoryRoot, "verification/opds/schemas"),
);
const schemaRoots = [
  path.join(snapshotRoot, "opds-2.0"),
  path.join(snapshotRoot, "readium-webpub/schema"),
];
const schemaIds = {
  feed: "https://specs.opds.io/schema/feed.schema.json",
  publication: "https://specs.opds.io/schema/publication.schema.json",
};

function jsonSchemaFiles(root) {
  const found = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const selected = path.join(root, entry.name);
    if (entry.isDirectory()) {
      found.push(...jsonSchemaFiles(selected));
    } else if (entry.isFile() && entry.name.endsWith(".schema.json")) {
      found.push(selected);
    }
  }
  return found.sort();
}

function schemaValidator() {
  // The upstream draft-07 corpus uses union-valued `type`, omits redundant
  // object types beside object-only keywords, and places `required` in
  // composition branches whose `properties` live in the parent schema.
  // Those documented strict-lint exceptions do not weaken validation.
  // Missing or remote $refs still fail synchronously because no asynchronous
  // or network loader exists.
  const ajv = new Ajv({
    allErrors: true,
    allowUnionTypes: true,
    strict: true,
    strictRequired: false,
    strictTypes: false,
  });
  addFormats(ajv);
  const schemas = schemaRoots.flatMap(jsonSchemaFiles).map((schemaPath) => {
    const schema = JSON.parse(fs.readFileSync(schemaPath, "utf8"));
    if (typeof schema.$id !== "string" || schema.$id.length === 0) {
      throw new Error(`schema has no absolute $id: ${schemaPath}`);
    }
    return schema;
  });
  for (const schema of schemas) {
    ajv.addSchema(schema);
  }
  // Compile every snapshot, not only the two public roots. This is the offline
  // closed-reference check and catches a missing extension schema immediately.
  for (const schema of schemas) {
    if (ajv.getSchema(schema.$id) === undefined) {
      throw new Error(`schema did not compile: ${schema.$id}`);
    }
  }
  return ajv;
}

function usage() {
  console.error(
    "usage: node scripts/validate-opds2.mjs --check-schemas | <feed|publication> <file|->",
  );
}

async function inputBytes(inputPath) {
  if (inputPath !== "-") {
    return fs.readFileSync(inputPath, "utf8");
  }
  const chunks = [];
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return chunks.join("");
}

async function main() {
  const arguments_ = process.argv.slice(2);
  let ajv;
  try {
    ajv = schemaValidator();
  } catch (error) {
    console.error(`OPDS 2 schema compilation failed: ${error.message}`);
    return 2;
  }
  if (arguments_.length === 1 && arguments_[0] === "--check-schemas") {
    return 0;
  }
  if (arguments_.length !== 2 || !(arguments_[0] in schemaIds)) {
    usage();
    return 2;
  }

  const [kind, inputPath] = arguments_;
  let document;
  try {
    document = JSON.parse(await inputBytes(inputPath));
  } catch (error) {
    console.error(`${inputPath}: JSON parse failed: ${error.message}`);
    return 1;
  }
  const validate = ajv.getSchema(schemaIds[kind]);
  if (validate === undefined) {
    console.error(`compiled ${kind} validator is unavailable`);
    return 2;
  }
  if (!validate(document)) {
    console.error(`${inputPath}: OPDS 2 ${kind} validation failed`);
    console.error(JSON.stringify(validate.errors, null, 2));
    return 1;
  }
  return 0;
}

process.exitCode = await main();
