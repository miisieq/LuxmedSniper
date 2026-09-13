# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

How to use LuxmedSniper?
--------------------
First of all create virtualenv and install Python requirements from requirements.txt

1) Copy the template and fill in your credentials. The copy is gitignored, the template is not
   - never put real credentials in `luxmed_sniper.example.yaml`:
```
cp luxmed_sniper.example.yaml luxmed_sniper.yaml
```
Every option is documented inline in the template: which ids go into `doctor_locators`,
which notification providers exist and what each one needs. City and service ids can be
looked up in `luxmed-ids/`, or regenerated with `python3 luxmed_sniper.py --dump-ids`.

Configuration for several people goes into separate files - name them `luxmed_<user>.yml`
or `local-<user>.yaml`, both of which are gitignored.

2) Run it
```
python3 luxmed_sniper.py
```
`luxmed_sniper.yaml` is the default, so `-c` is only needed for a different path. Several
files can be merged, which is handy for splitting users, doctors and providers apart:
```
python3 luxmed_sniper.py -c user_config.yml luxmed_john.yml
```
Note that merging cannot override a value - a key present in two files must hold the same
value in both, otherwise the script stops with a conflict error.

3) Wait for new appointment notifications in your pushover app on mobile :)!

# Warning

Please be advised that running too many queries against LuxMed API may result in locking your LuxMed account.
Breaching the 'fair use policy' for the first time locks the account temporarily for 1 day.
Breaching it again locks it indefinitely and manual intervention with "Patient Portal Support" is required to unlock it.
