# Aroma-Link Pro300: protokół MQTT (reverse-engineered)

Zdekodowane z `captures/*.pcap` (tcpdump na UDM br40, pełne sterowanie z chmury
2026-10-31). Transport: **MQTT, TCP/1883, bez TLS**.

## Topici

```
q/1/d/qdpu118T<mac>/bus   downlink  server -> device   (komendy)
q/2/d/qdpu118T<mac>/bus   uplink    device -> server   (stan / ACK)
```
`<mac>` = MAC bez dwukropków, lowercase. Tu: `aabbccddeeff`. Prefiks `qdpu118T` potwierdzony
przy CONNECT (clientid = topic-id, patrz niżej).

## Koperta transportowa (payload MQTT)

```
AA AA | len16 | cksum8 | msgid16 | tag16 | <INNER-ASCII-HEX>
```
- `AA AA` stałe.
- `len16` = liczba bajtów PO tym polu (cksum+msgid+tag+inner).
- `cksum8` = **suma kontrolna**: `(suma bajtów msgid16 + tag16 + ASCII-hex) & 0xFF`. Potwierdzona na
  28/28 ramkach chmury (2026-11-01). Wcześniej błędnie opisana jako licznik `seq`; ramka ze złą sumą
  jest PUBACK-owana przez stos MQTT urządzenia, ale odrzucana aplikacyjnie (bez odpowiedzi).
- `msgid16` = licznik wiadomości, inkrementuje. Powtórzenie już widzianego msgid (replay) jest
  ignorowane, więc lokalny nadawca musi startować wysoko i rosnąć monotonicznie.
- `tag16` = **0x0023 dla downlink, 0x0024 dla uplink** (stałe).
- `INNER-ASCII-HEX` = ramka właściwa jako tekst heks (patrz niżej).

## Ramka wewnętrzna

```
AA | dir | flag | MAC(6B) | 00 | cmd | sub | 01 | 00 | payload... | XOR | EE
```
- `AA` start, `EE` koniec.
- `dir`: `AC` = downlink (komenda), `CA` = uplink (odpowiedź/stan).
- `flag`: `00` downlink, `01` uplink.
- `MAC(6B)`: `AA BB CC DD EE FF`.
- `cmd`: `0x57` = control (zapis), `0x52` = query (odczyt).
- `sub`: podkomenda (patrz tabela).
- `XOR` = **XOR wszystkich bajtów od `AA`(start) do bajtu tuż przed XOR** (potwierdzone na 7 próbkach, algorytm w `bridge/aroma_codec.py`).

## Komendy (zweryfikowane)

| Operacja | dir | cmd | sub | payload | przykład inner | źródło |
|---|---|---|---|---|---|---|
| Power ON | down | 57 | 10 | `01 00 01` | `AAAC00<MAC>00571001000116EE` | pcap 20:37:42 |
| Power OFF | down | 57 | 10 | `01 00 00` | `AAAC00<MAC>00571001000017EE` | pcap 20:38:11 |
| Fan ON | down | 57 | 11 | `01 00 01` | `AAAC00<MAC>00571101000117EE` | pcap 20:38:40 |
| Fan OFF | down | 57 | 11 | `01 00 00` | `AAAC00<MAC>00571101000016EE` | pcap 20:39:09 |
| Schedule write | down | 57 | 15 | `len + 5×slot` | `AAAC00<MAC>0057152e...EE` | pcap 20:39:38 (workSet TEST) |
| Query state/sched | down | 52 | 15 | `02 00 01 1F` | `AAAC00<MAC>0052150200011F0AEE` | pcap (poll) |
| ACK komendy (nie stan) | up | 57 | 10/11 | `01 00 00` (zawsze 00) | `AACA01<MAC>00571101000071EE` | pcap + lokalnie 2026-11-01 |
| Stan power | up | 53 | 10 | `01 00 val` | `AACA01<MAC>00531001000175EE` | pcap full-control (po power ON/OFF), 2 ramki |
| Stan fan | up | 53 | 11 | `01 00 val` | `AACA01<MAC>00531101000174EE` | pcap (fan=1), lokalnie ON/OFF |
| Dump harmonogramu | up | 52/53 | 15/2E | 5×slot | `AACA01<MAC>0052152E...EE` | pcap |
| Raport stanu | down/up | 52 | 25 | zapytanie `02 00 01 1F`; odpowiedź patrz niżej | `AACA01<MAC>00522517...EE` | pcap + żywe sondowanie 2026-11-01 |

### Slot harmonogramu (sub 0x15, dług. 0x2E=46 dla 5 slotów)

Blok na slot (zaobserwowany w workSet TEST = slot0 00:00-23:59, poziom2, work7, pause123):
```
startH startM endH endM | flags | workSec(2B BE) | pauseSec(2B BE)
00     00     17   3b    | 21    | 00 07          | 00 7b
```
- czas: `17 3b` = 23:59 (hex godzina/minuta).
- `flags`: hi-nibble = consistenceLevel (1-3), lo-nibble = enabled (0/1). `21` = poziom2+on, `10` = poziom1+off.
- workSec/pauseSec: big-endian 16-bit, w sekundach (TEST: 7 / 123; filler: 10 / 120).

## CONNECT (zdekodowany z power-cycle, captures/power-cycle-connect-20261031.pcap)

Po reboocie urządzenie zrobiło **DNS na `iot-south.landecia.com`** (przez UDM <gateway>) i połączyło
się z **<vendor-ip>:1883** (inny IP niż wcześniejsze <vendor-ip> → broker rozwiązywany po HOSTNAME,
nie hardkod IP). Pakiet CONNECT (MQTT 3.1.1):

| Pole | Wartość |
|---|---|
| proto / level | MQTT / 4 (3.1.1) |
| keepalive | 120 s |
| clean_session | true |
| clientid | `qdpu118Taabbccddeeff` (= topic-id) |
| username | brak |
| password | `1;<seq>;<salt>;<signature-redacted>` (token podpisany; pole 2 = licznik, prawdopodobnie rotuje per połączenie) |
| LWT (will) | brak |
| SUBSCRIBE | `q/1/d/qdpu118Taabbccddeeff/+` QoS 1 |

Konsekwencja dla cutoveru: rotujące hasło → **nie** dodawać statycznego usera do built_in_database;
użyć **listenera EMQX bez authn** dla tego urządzenia (ACL dalej ogranicza topici). Patrz docs/cutover.md.

## Topici uplinku

- `q/2/d/<id>/bus`, stan/komendy (ramki AA..EE jak wyżej).
- `q/2/d/<id>/sys`, telemetria tekstowa (nie format AA..EE): app 1.0.5, MCU, wersje modułów,
  `AUTO;LBS;WIFI`. Integracja łapie oba (`q/2/d/+/+`), /sys na razie ignoruje.

## Ramki startowe (boot announce)

- `53/F1`, tożsamość: ASCII z `virtualImei=<imei-redacted>`, `V1.0.20250416`, `A6/Pro300_2_SEG`.
- `53/06`, krótki status po starcie (wartość 00).

Brak NTP (UDP 123) w oknie power-cycle; źródło czasu dla harmonogramu niepotwierdzone.


## Raport stanu 52/25 (zweryfikowany na żywo 2026-11-01)

Zapytanie: `AAAC00<MAC>00 52 25 02 00 01 1F <XOR> EE` (te same cztery bajty co `52/15`). Odpowiedź:

```
17 00 | rok16 mies dzień godz min sek dow | 00 power faza | workRemain16 pauseRemain16 |
        startH startM endH endM poziom | 00 00 00
```
- Zegar to czas **lokalny** urządzenia; `dow` 1=pn … 7=nd (zgodne z sobotą `06` w przechwycie z 31.10).
- `power` 0/1; `faza` 0 wyłączony / 1 rozpyla / 2 pauza (jak `workStatus` w chmurze). Kolejność potwierdzona próbką w fazie pauzy: `00 01 02`, pauza 107 s.
- `workRemain` i `pauseRemain` w sekundach, odliczają przy włączonym zasilaniu (10 → 4 s w odstępie 6 s);
  przy wyłączonym stoją na wartościach slotu (10/120).
- Wentylator nie jest w tym raporcie; jego stan to `53/11`.

## Slot: jeden zestaw, nie tygodniowy (sondowanie 2026-11-01)

Zapytanie `52/15` ma payload `02 <b1> <b2> 1F`. Odpowiedź echuje `b2` w nagłówku (`2E 00 <b2>`), więc to
identyfikator zapytania (testowane 01 i 02), a `1F` nie ma wpływu. `b1` inne niż `00` (testowane 1 do 7)
nie dostaje żadnej odpowiedzi. Wniosek: urządzenie trzyma **jeden zestaw 5 slotów** bez dnia tygodnia;
harmonogram tygodniowy musi być realizowany po stronie HA (zapis slotów na dany dzień o północy).


## Siła sygnału (RSSI): nie dostępna lokalnie (stan 2026-11-01)

Sensor `Signal Strength` integracji chmurowej nie ma odpowiednika: w ramkach TLV `/sys` (moduł, aplikacja,
MCU, wersje) nie ma wartości sygnału, a raport `52/25` jej nie niesie. Binarna ramka `/sys` z bootu
(`000a 0064 0012 7e11c379 37e0 8001 001a 8071 0022 020a e000` po 9-bajtowym nagłówku) ma niezidentyfikowane
pola (`0x64` = 100 to kandydat na wartość procentową); z jednej próbki nie da się tego rozstrzygnąć.
Integracja zapisuje tę ramkę jako atrybut `boot_binary` sensora wersji, żeby porównać kolejne boty
przy zmienionych warunkach radiowych, oraz wystawia zdarzenie `aroma_link_local_frame` i serwis `send_frame`
do badania kolejnych komend (np. nieznanych `52/xx`).
