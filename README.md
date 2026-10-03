# Stash

Itse ylläpidetty kirjanmerkkien hallinta, jota voi käyttää selaimella, toiselta koneelta API:n kautta ja
luonnollisella kielellä paikallisen kielimallin avulla.

**Kaikki pysyy omissa käsissä.** Stash pyörii omalla palvelimellasi, ja kirjanmerkit, selaushistoria ja
muutoshistoria ovat sinun tietokannassasi. Kielimalli toimii omalla koneellasi (Ollama), joten mikään
pilvipalvelu ei näe, mitä tallennat, mitä luet tai mitä mallilta kysyt. Juuri tämä on itse hostauksen iso
etu: selaushistoriasta saa hyödyn irti luovuttamatta sitä kenellekään.

## Osat

1. **Palvelinsovellus** (`app/`, `static/`): selaimella käytettävä kirjanmerkkien hallinta.
   - **Työpöytä**: välilehdet, joilla kategoriat sarakkeissa ja kirjanmerkit niiden sisällä, raahaamalla järjestettävä.
   - **Katalogi**: tunnisteilla (tageilla) järjestetty varasto ja nopea haku.
   - Tuonti ja vienti selainten kirjanmerkkitiedostona, välilehtien jakaminen linkillä, duplikaattien ja
     kuolleiden linkkien etsintä, kirjanmerkkisovelma, selainlaajennus, kaksivaiheinen tunnistautuminen,
     kutsuihin perustuva rekisteröityminen, suomi ja englanti.
   - **API** (`/api/v1`) API-avaimilla: kaikki kirjanmerkit kerralla, haku ja muutokset esikatseluineen.
     Jokaisen muutoksen voi kumota.
2. **stashai** (`client/`): Linuxin pääteohjelma, jolle annetaan ohjeita luonnollisella kielellä
   ("siirrä kaikki firmaan X liittyvät tagille Y", "tarkista toimivatko nämä linkit vielä"). Omalla koneella
   toimiva kielimalli tutkii kirjanmerkit API:n kautta, voi lukea verkkosivuja ja selaushistoriaa ja
   ehdottaa muutoksia. Mitään ei muuteta ennen kuin hyväksyt esikatselun, ja kaiken voi kumota.
   Ks. [`client/README.md`](client/README.md).
3. **Selaushistorian vienti** (`scripts/stash-history-sync.py`): pieni skripti (macOS ja Linux, pelkkä
   Python 3), joka lähettää Firefoxin selaushistorian käyntimäärät Stashiin muutaman tunnin välein.
   Silloin stashai näkee, mitä kirjanmerkkejä oikeasti käytät: se voi tuoda käytetyimmät työpöydälle,
   järjestää kirjanmerkit ja kategoriat käytön mukaan, löytää usein käytetyt sivut, joita ei ole vielä
   tallennettu, ja ehdottaa vuosiin käyttämättömien siivoamista. Historia tallentuu vain omalle
   palvelimellesi, ei kuulu kirjanmerkkien vientiin ja sen voi poistaa laitekohtaisesti asetuksista.
   Ennen lähetystä osoitteista poistetaan salaisuuksia usein sisältävät parametrit (token, session, code…),
   ja sivustoja voi jättää kokonaan pois.

## Rakenne

| Polku | Sisältö |
| --- | --- |
| `app/main.py` | FastAPI-rajapinta: tilit, välilehdet, kategoriat, kirjanmerkit, haku, jako, tuonti/vienti |
| `app/db.py` | SQLite-skeema ja yhteydet (`data/stash.db`, WAL) |
| `app/net.py` | Ulospäin lähtevät haut (kuolleet linkit, otsikot, favicon-välimuisti) – vain julkisiin osoitteisiin |
| `app/security.py` | Salasanat (scrypt), istunnot, TOTP, yritysten rajoitin |
| `app/api_v1.py` | API-avaimilla käytettävä `/api/v1` sekä avainten ja muutoshistorian hallinta |
| `app/history.py` | Laitteiden lähettämä selaushistoria: vastaanotto, käyntimäärät kirjanmerkeille, haku |
| `client/` | stashai-pääteohjelma (oma Python-paketti) |
| `scripts/stash-history-sync.py` | Firefoxin historian lähetys Stashiin (jaetaan osoitteessa `/dl/stash-history-sync.py`) |
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
| `POST /api/v1/history/{laite}` | `{"items": [...], "reset": true, "done": true}` – laitteen selaushistoria (korvaa aiemman) |
| `GET /api/v1/history?q=&host=&min_visits=&period=&bookmarked=` | käydyt osoitteet käytetyimmät ensin, ja mihin kirjanmerkkeihin ne osuvat |
| `GET /api/v1/history/usage`, `GET /api/v1/history/sources`, `DELETE /api/v1/history/{laite}` | kirjanmerkkien käyntimäärät, laitteet, poisto |

Operaatiot: `add_tags`, `remove_tags`, `set_tags` (`ids`, `tags`), `rename_tag` (`old`, `new`; tyhjä
`new` poistaa tagin), `delete` (`ids`), `move` (`ids` + `category_id` tai `tab`+`category`, puuttuvat
luodaan, tai `catalog: true`), `update` (`id` + `title`/`url`/`notes`/`color`/`tags`), `create`
(`url`, `title`, `tags`, paikka kuten `move`), `order_bookmarks` (kategorian kirjanmerkit annettuun
järjestykseen), `order_categories` (välilehden kategoriat järjestykseen, kukin sarakkeensa sisällä). `dry_run` ajaa operaatiot ja peruu ne, joten esikatselu
(`diff`: jokaisen kirjanmerkin tagit, paikka ja kentät ennen/jälkeen) on täsmälleen se mitä tapahtuisi.
Oikea ajo tallentaa muutosjoukon (`changesets`, 200 viimeisintä) kosketettujen kirjanmerkkien aiemman tilan
kanssa; kumoaminen palauttaa ne, tuo poistetut takaisin ja poistaa lisätyt. Jos myöhempi muutos koski
samoja kirjanmerkkejä, kumoaminen vaatii `force=true`. Muutokset näkyvät ja kumoutuvat myös kohdassa
Asetukset → API-avaimilla tehdyt muutokset.

Selaushistoria yhdistetään kirjanmerkkeihin väljällä osoitevertailulla (http/https, `www.` ja
loppukauttaviiva eivät vaikuta). Jokainen laite lähettää osoitekohtaiset käyntimäärät (30, 90 ja 365
päivää sekä kaikki), ja uusi lähetys korvaa saman laitteen aiemmat rivit.

`client/` on **stashai**, Linuxin pääteohjelma, joka tekee pyyntöjä luonnollisella kielellä paikallisen
kielimallin avulla (ks. `client/README.md`). Paketti rakennetaan komennolla
`client/.venv/bin/pip wheel --no-deps -w client/dist client/`, ja Stash jakaa sen osoitteessa
`/dl/stashai-<versio>-py3-none-any.whl` (asetussivu näyttää uusimman `pipx install` -komennon).

Historian lähetys Macilta (tai Linuxilta), jolla Firefoxia käytetään:

```sh
curl -o ~/stash-history-sync.py https://stash.example.com/dl/stash-history-sync.py
python3 ~/stash-history-sync.py --setup       # osoite, API-avain (muutosoikeus), koneen nimi
python3 ~/stash-history-sync.py --dry-run     # näyttää mitä lähetettäisiin
python3 ~/stash-history-sync.py --schedule    # lähettää 6 tunnin välein (launchd / cron)
```

## Testit

```sh
.venv/bin/python tests/smoke.py    # rajapinta päästä päähän, väliaikainen tietokanta
.venv/bin/python tests/api_v1.py   # API-avaimet, /api/v1, esikatselu ja kumoaminen
.venv/bin/python tests/history.py  # historiaskripti tekaistua Firefox-profiilia vasten, historia-API, järjestäminen
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
