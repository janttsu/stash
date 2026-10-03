# stashai

Stash-kirjanmerkkien hallinta luonnollisella kielellä Linuxin päätteestä. Kirjoitat mitä haluat, omalla
koneellasi toimiva kielimalli (Ollama, oletuksena `qwen3.6:35b-a3b`) selvittää miten se tehdään, näyttää
tarkalleen mitä muuttuisi ja muuttaa kirjanmerkkejä vasta kun hyväksyt. Malli toimii omalla koneellasi, joten
kirjanmerkkisi, selaushistoriasi ja pyyntösi eivät päädy millekään pilvipalvelulle.

```
> listaa kaikki mikä liittyy firmaan X
> siirrä kaikki firmaan X liittyvät asiat tagille y
> mitkä kirjanmerkit ovat ilman tageja? ehdota niille tagit
> poista ne
> tarkista toimivatko linux-tagin linkit vielä ja korjaa siirtyneet
> mitä kirjanmerkin 1234 takana on? tagita se kunnolla
> tuo eniten käyttämäni kirjanmerkit työpöydälle ja järjestä käytetyimmät ylös
> mitä sivuja käytän usein mutta en ole tallentanut?
> mitkä työpöydän kirjanmerkit ovat olleet käyttämättä vuoden?
```

## Asennus

Tarvitset Pythonin 3.11+, pipx:n ja Ollaman, jossa on malli:

```sh
ollama pull qwen3.6:35b-a3b
pipx install --force https://stash.example.com/dl/stashai-0.1.4-py3-none-any.whl   # tarkka osoite: Stash → Asetukset → API-avaimet
stashai login https://stash.example.com      # liitä avain, jolla on muutosoikeus
stashai doctor                                # tarkistaa Stashin, mallin ja kontekstin koon
stashai                                       # käyttöliittymä
stashai update                                # päivittää uusimpaan versioon (Stash jakaa sen)
```

Versiot 0.1.1 alkaen osaavat päivittää itsensä. Vanhemman version päivität kerran käsin:
`pipx install --force https://stash.example.com/dl/stashai-<versio>-py3-none-any.whl`. stashai kertoo
käynnistyessään ja `stashai doctor` -komennossa, kun uudempi versio on saatavilla.

Malli haetaan samasta Ollamasta kuin `ollama`-komento käyttää: `[llm] url` tiedostossa `config.toml`,
muuten `OLLAMA_HOST`, muuten `127.0.0.1:11434`.

API-avaimen saat Stashista: Asetukset → API-avaimet → Luo API-avain. Avain tallentuu tiedostoon
`~/.config/stashai/config.toml`, jota vain sinä voit lukea (tai anna se ympäristömuuttujassa `STASHAI_KEY`).

## Käyttö

- Vasemmalla keskustelu ja mallin työvaiheet, oikealla tulokset ja muutosehdotukset.
- Ehdotus näyttää jokaisen muutoksen (tagit +/-, siirrot, poistot). **y** tai tyhjä Enter toteuttaa,
  **n** hylkää, ja voit myös kirjoittaa tarkennuksen ("älä poista niitä joissa on tagi x").
- **Esc** pysäyttää mallin, **PgUp/PgDn** vierittää oikeaa ruutua, **F1** ohje, **Ctrl+Q** lopettaa.
- Komennot: `/undo [ID] [force]`, `/history`, `/sets`, `/show S3`, `/refresh`, `/new`, `/rules`, `/model NIMI`.

Ilman käyttöliittymää: `stashai ask "pyyntö"` (kysyy ennen muutoksia, `--yes` toteuttaa suoraan),
`stashai history`, `stashai undo [ID]`.

### Omat säännöt

`~/.config/stashai/rules.md` sisältää pysyvät sääntösi vapaalla kielellä, esimerkiksi
"reseptit saavat aina tagin ruoka eivätkä muita tageja". Malli noudattaa niitä jokaisessa ehdotuksessa.
Tiedosto luetaan jokaisen pyynnön alussa.

## Miten se toimii

Periaatteet on otettu sortosta:

- **Malli vain ehdottaa.** Se käyttää lukutyökaluja (haku, listaus, tarkistus) ja lopuksi joko vastaa tai
  ehdottaa muutoksia. Ohjelma tarkistaa ehdotuksen, ja Stash ajaa sen kuivana (`dry_run`), joten esikatselu
  on täsmälleen se mitä tapahtuisi. Mitään ei muuteta ennen hyväksyntää.
- **Joukot nimillä, ei id-listoina.** Jokainen haku tekee joukon (S1, S2, …), ja malli viittaa niihin
  nimellä ("poista S4"). Ohjelma laajentaa nimet id:iksi, joten malli ei voi kadottaa tai keksiä kirjanmerkkejä.
  "Ne" tarkoittaa viimeksi näytettyä joukkoa.
- **Kaikki kerralla tarvittaessa.** Kaikki kirjanmerkit ladataan kerralla koneelle, joten haut ovat
  välittömiä. Kun sanat eivät riitä (aihe, merkitys, kieli), `judge` antaa mallin lukea joukon läpi
  60 kirjanmerkin erissä; `ALL` käy läpi koko kokoelman (hidasta: noin 80 mallikutsua 4 700 kirjanmerkille).
- **Verkko.** Malli voi lukea sivun tekstinä (`fetch`: tila, ohjaukset, otsikko, kuvaus, otsikot, teksti),
  tarkistaa kerralla koko joukon linkit (`check`: elossa / siirtynyt / kuollut / epäselvä) ja hakea verkosta
  (`web_search`: DuckDuckGo tai oma SearXNG). Siirtyneet linkit saa päivitettyä uuteen osoitteeseensa
  (`update_urls`). Etusivulle tai kirjautumiseen ohjautuva linkki on "epäselvä" eikä "siirtynyt", ja
  "kuollut" tarkoittaa vain 404/410:tä, olematonta verkkotunnusta tai hylättyä yhteyttä. Haut tehdään omalta
  koneelta (VPN:n takana olevat sivut toimivat), mutta tämän koneen omiin osoitteisiin ei mennä koskaan eikä
  lähiverkkoon ilman asetusta `allow_private`. Sivujen teksti on dataa, ei ohjeita: malli ei saa totella
  sivulla olevia käskyjä, ja vaikka yrittäisi, tuloksena on vain ehdotus, jonka näet ennen hyväksyntää.
  Verkkohaun kyselyt menevät DuckDuckGolle; `[web] search = "off"` tai oma SearXNG-osoite vaihtaa sen.
- **Selaushistoria.** Kun Firefoxin historia on lähetetty Stashiin (`stash-history-sync.py`, ks. Stash →
  Asetukset → Selaushistoria), jokaisella kirjanmerkillä näkyy mallille käyntimäärät (30 / 90 päivää / kaikki)
  ja viimeisin käynti. Haussa voi rajata käytön mukaan (`used_min`, `unused_days`, `sort: "use"`), `history`
  listaa käydyt osoitteet (myös ne, joita ei ole tallennettu), ja `sort_by_use` järjestää välilehden
  kirjanmerkit ja kategoriat käytetyimmät ensin. Järjestyksen laskee ohjelma käyntimääristä, joten se on aina
  johdonmukainen. Malli päättää vain, mitä nostetaan työpöydälle ja mihin.
- **Kumottavissa.** Jokainen toteutettu muutos tallentuu Stashiin muutosjoukoksi, jossa on kosketettujen
  kirjanmerkkien aiempi tila. `/undo` (tai Stashin Asetukset → API-avaimilla tehdyt muutokset) palauttaa
  tagit, paikat ja tiedot, tuo poistetut takaisin ja poistaa lisätyt.
- **Vain paikallinen malli.** Mallin osoitteen pitää olla tämä kone (loopback); välityspalvelinasetuksia ei
  käytetä. Kirjanmerkit eivät lähde pilvimalleille.
- **Loki.** Jokaisesta istunnosta tallentuu `~/.local/state/stashai/logs/session-*.log` (pyynnöt, mallin
  vastaukset, työkalujen tulokset, ehdotukset ja toteutetut muutokset).

Asetukset (`[llm]` tiedostossa `config.toml`): `url`, `model`, `num_ctx` (0 = palvelimen
`OLLAMA_CONTEXT_LENGTH`; alle 16 384 tokenin kontekstilla stashai pyytää 32 768), `think` (Qwenin
piilopäättely: parempia suunnitelmia, paljon hitaammin), `judge_batch`, `max_steps`, `keep_alive`.
`[web]`: `enabled` (true), `search` (`"duckduckgo"`, SearXNG-osoite tai `"off"`), `allow_private` (false).

## Kehitys

```sh
cd ~/stash/client
python -m venv .venv && .venv/bin/pip install -e '.[dev]' fastapi pydantic
.venv/bin/python -m pytest -q        # ajaa agentin oikeaa Stash-sovellusta vasten (väliaikainen tietokanta)
.venv/bin/pip wheel --no-deps -w dist .   # paketti, jonka Stash jakaa osoitteessa /dl/
```
