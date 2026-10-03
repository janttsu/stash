# Stash

Itse ylläpidetty kirjanmerkkien hallinta (Bookmark Ninjan tapaan): **Työpöytä** (välilehdet → kategoriat sarakkeissa →
kirjanmerkit) ja **Katalogi** (tunnisteilla järjestetty varasto).

## Rakenne

| Polku | Sisältö |
| --- | --- |
| `app/main.py` | FastAPI-rajapinta: tilit, välilehdet, kategoriat, kirjanmerkit, haku, jako, tuonti/vienti |
| `app/db.py` | SQLite-skeema ja yhteydet (`data/stash.db`, WAL) |
| `app/net.py` | Ulospäin lähtevät haut (kuolleet linkit, otsikot, favicon-välimuisti) – vain julkisiin osoitteisiin |
| `app/security.py` | Salasanat (scrypt), istunnot, TOTP, yritysten rajoitin |
| `app/api_v1.py` | API-avaimilla käytettävä `/api/v1` sekä avainten ja muutoshistorian hallinta |
| `client/` | stashai-pääteohjelma (oma Python-paketti) |
| `app/importer.py` | Selainten kirjanmerkkitiedoston (Netscape HTML) luku ja kirjoitus |
| `static/` | Käyttöliittymä: natiivit ES-moduulit, ei käännösvaihetta |
| `extension/` | Selainlaajennus (ladataan sovelluksesta `/extension.zip`) |
| `data/` | Tietokanta ja favicon-välimuisti – ei versionhallinnassa, varmuuskopioi tämä |

## Ylläpito

```sh
systemctl --user status stash          # palvelu (uvicorn, käänteisvälityspalvelin edessä)
systemctl --user restart stash         # koodimuutosten jälkeen (static/-muutokset eivät vaadi)
journalctl --user -u stash -f          # lokit

cd ~/stash
.venv/bin/python -m app.cli invite                  # kertakäyttöinen kutsulinkki (myös ensimmäiselle tilille)
.venv/bin/python -m app.cli users                   # tilit
.venv/bin/python -m app.cli reset-password NIMI     # uusi satunnainen salasana, 2FA pois
.venv/bin/python -m app.cli registration invite     # open | invite | closed
```

Ensimmäisenä luotu tili on ylläpitäjä. Rekisteröityminen on oletuksena vain kutsulinkillä; ylläpitäjä luo
kutsuja ja vaihtaa tilaa kohdasta Asetukset → Käyttäjät ja rekisteröityminen.

Ympäristömuuttujat: `STASH_ORIGIN` (julkinen osoite), `STASH_DATA` (datahakemisto, oletus `./data`).

## API ja stashai

Muut koneet ja ohjelmat käyttävät Stashia osoitteessa `/api/v1` API-avaimella
(`Authorization: Bearer stash_…`). Avaimet luodaan kohdassa Asetukset → API-avaimet; tietokantaan
tallentuu vain avaimen tiiviste. Avain on vain luku -avain, ellei sille sallita muutoksia. `/api/v1` ei
hyväksy istuntoevästettä, joten se ei tarvitse CSRF-otsaketta.

| Kutsu | Mitä tekee |
| --- | --- |
| `GET /api/v1/me` | käyttäjä, avaimen nimi ja oikeudet, kirjanmerkkien määrä |
| `GET /api/v1/snapshot` | kaikki kirjanmerkit kerralla + työpöydän rakenne + tagit |
| `GET /api/v1/bookmarks?q=&tags=&mode=&host=&untagged=&scope=&ids=&limit=&offset=` | haku (enintään 1000 kerralla) |
| `GET /api/v1/tags`, `GET /api/v1/structure` | tagit määrineen, välilehdet ja kategoriat |
| `POST /api/v1/changes` | `{"ops": [...], "summary": "...", "dry_run": true}` – muutokset yhtenä transaktiona |
| `GET /api/v1/changes`, `POST /api/v1/changes/{id}/undo[?force=true]` | muutoshistoria ja kumoaminen |

Operaatiot: `add_tags`, `remove_tags`, `set_tags` (`ids`, `tags`), `rename_tag` (`old`, `new`; tyhjä
`new` poistaa tagin), `delete` (`ids`), `move` (`ids` + `category_id` tai `tab`+`category`, puuttuvat
luodaan, tai `catalog: true`), `update` (`id` + `title`/`url`/`notes`/`color`/`tags`), `create`
(`url`, `title`, `tags`, paikka kuten `move`). `dry_run` ajaa operaatiot ja peruu ne, joten esikatselu
(`diff`: jokaisen kirjanmerkin tagit, paikka ja kentät ennen/jälkeen) on täsmälleen se mitä tapahtuisi.
Oikea ajo tallentaa muutosjoukon (`changesets`, 200 viimeisintä) kosketettujen kirjanmerkkien aiemman tilan
kanssa; kumoaminen palauttaa ne, tuo poistetut takaisin ja poistaa lisätyt. Jos myöhempi muutos koski
samoja kirjanmerkkejä, kumoaminen vaatii `force=true`. Muutokset näkyvät ja kumoutuvat myös kohdassa
Asetukset → API-avaimilla tehdyt muutokset.

`client/` on **stashai**, Linuxin pääteohjelma, joka tekee pyyntöjä luonnollisella kielellä paikallisen
kielimallin avulla (ks. `client/README.md`). Paketti rakennetaan komennolla
`client/.venv/bin/pip wheel --no-deps -w client/dist client/`, ja Stash jakaa sen osoitteessa
`/dl/stashai-<versio>-py3-none-any.whl` (asetussivu näyttää uusimman `pipx install` -komennon).

## Testit

```sh
.venv/bin/python tests/smoke.py    # rajapinta päästä päähän, väliaikainen tietokanta
.venv/bin/python tests/api_v1.py   # API-avaimet, /api/v1, esikatselu ja kumoaminen
client/.venv/bin/python -m pytest -q client/tests   # stashai: haku, agentti, malliyhteys, TUI
node tests/ui.mjs                  # headless Chromium, oma palvelin portissa 8013, kuvat → data/tmp/
node tools/check-i18n.mjs          # puuttuvat suomennokset
```

## Tietoturvasta lyhyesti

- Istuntoeväste on `HttpOnly; Secure; SameSite=Lax`; jokainen muuttava API-kutsu vaatii `X-Stash`-otsakkeen (CSRF).
- CSP sallii vain oman alkuperän skriptit ja tyylit; kirjanmerkkien osoitteista hyväksytään http(s), ftp, mailto ja tel.
- Palvelimen tekemät haut selvittävät ensin kohteen IP:n ja kieltäytyvät sisäverkon/localhostin osoitteista.
- Kirjanmerkkisovelma ja laajennus välittävät sivun tiedot URL:n `#`-osassa, joka ei päädy palvelimen lokeihin;
  välityspalvelimen loki kannattaa kirjoittaa ilman kyselymerkkijonoja ja evästeitä.
