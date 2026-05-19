# local/ — drop zone for a hand-crafted demo profile

This directory lets you run the ad-performance skill against a fixed warehouse without going through Fivetran setup. Useful for demos.

## Usage

1. Copy the template to the standard profile path:
   ```sh
   cp .marketplace/fivetran-skills/skills/ad-performance/local/profile.example.json \
      ~/.fivetran/skills/ad-performance/profile.json
   ```
   (Or set `AD_PERFORMANCE_PROFILE_PATH=/some/other/path.json` and copy there.)

2. Edit the copy. Replace every `<YOUR_GCP_PROJECT_ID>` and `<YOUR_DATASET>` with your demo BigQuery project and dataset (the dataset that contains the `ad_reporting__*` multisource QDM tables). Remove any connector entries that don't have data in your dataset.

3. Invoke the skill normally — `validate` passes, `resolve`/`readiness` operate against the demo dataset.

To return to a normal Fivetran-driven setup: `rm ~/.fivetran/skills/ad-performance/profile.json` and re-invoke the skill.

## Why this directory exists

`profile.json` (the populated copy) is gitignored because it contains warehouse identifiers we don't want in a public repo. Only `profile.example.json`, this README, and the `.gitignore` are tracked.

See the "Demo / preconfigured profile" section in `skills/ad-performance/SKILL.md` for more.
