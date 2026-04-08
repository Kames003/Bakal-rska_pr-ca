"""
v poradi druhe parsovanie jedno sa deje na strane servera heduria je to z raw datas hex64 na json
Transformuje JSON z MQTT brokera na Python dict s GPS dátami
validačne funkcie
štatistiky

"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class GPSParser:
    """Parser for SenseCap tracker MQTT messages"""

    # Measurement IDs podľa SenseCap dokumentácie
    MEASUREMENT_IDS = {
        'LONGITUDE': '4197',
        'LATITUDE': '4198',
        'BATTERY': '3000',
        'EVENT_STATUS': '4200',
        'FIRMWARE': '3502',
        'HARDWARE': '3001'
    }

    # Validačné limity
    RSSI_MIN = -120  # dBm
    RSSI_MAX = 0  # dBm
    SNR_MIN = -20  # dB
    SNR_MAX = 20  # dB
    BATTERY_MIN = 0  # %
    BATTERY_MAX = 100  # %

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

        # Statistics tracking
        self.stats = {
            'total_messages': 0,
            'gps_messages': 0,
            'non_gps_messages': 0,
            'parse_errors': 0,
            'invalid_coordinates': 0,
            'invalid_battery': 0
        }

    def parse_message(self, topic: str, payload: str) -> Optional[Dict[str, Any]]:
        """
        Parsuje MQTT správu a extrahuje GPS dáta

        Args:
            topic: MQTT topic (napr. 'application/SENSECAP/decoded')
            payload: JSON payload ako string

        Returns:
            Dict s GPS dátami alebo None ak správa neobsahuje GPS
        """
        self.stats['total_messages'] += 1

        try:
            data = json.loads(payload)

            # Kontrola či je to decoded správa
            if 'decoded' not in data:
                self.logger.debug(f"Správa neobsahuje decoded data: {topic}")
                self.stats['non_gps_messages'] += 1
                return None

            decoded = data.get('decoded', {}).get('data', {})
            messages = decoded.get('messages', [[]])

            if not messages or not messages[0]:
                self.logger.debug("Prázdne messages pole")
                self.stats['non_gps_messages'] += 1
                return None

            # Extrakcia GPS súradníc z messages array
            measurements = messages[0]

            gps_data = self._extract_gps_coordinates(measurements)

            if gps_data:
                # Pridanie metadát z original správy
                original = data.get('original', {})
                gps_data.update({
                    'device_eui': original.get('devEUI'),
                    'device_name': original.get('deviceName'),
                    'timestamp': original.get('timestamp'),
                    'fcnt': original.get('fCnt'),
                    'rssi': self._extract_rssi(original),
                    'snr': self._extract_snr(original)
                })

                self.stats['gps_messages'] += 1
                self.logger.info(
                    f"GPS parsed: {gps_data['device_name']} - "
                    f"Lat: {gps_data['latitude']:.6f}, Lon: {gps_data['longitude']:.6f} - "
                    f"Battery: {gps_data['battery']}%"
                )
                return gps_data
            else:
                self.stats['non_gps_messages'] += 1

            return None

        except json.JSONDecodeError as e:
            self.stats['parse_errors'] += 1
            self.logger.error(f"JSON decode error: {e}")
            return None
        except Exception as e:
            self.stats['parse_errors'] += 1
            self.logger.error(f"Parse error: {e}", exc_info=True)
            return None

    def _extract_gps_coordinates(self, measurements: list) -> Optional[Dict[str, Any]]:
        """
        Extrahuje GPS súradnice z measurements array

        Args:
            measurements: List measurement objektov

        Returns:
            Dict s GPS dátami alebo None
        """
        latitude = None
        longitude = None
        battery = None
        timestamp_ms = None

        for measurement in measurements:
            measurement_id = measurement.get('measurementId')
            value = measurement.get('measurementValue')
            ts = measurement.get('timestamp')

            if measurement_id == self.MEASUREMENT_IDS['LATITUDE']:
                latitude = value
                timestamp_ms = ts
            elif measurement_id == self.MEASUREMENT_IDS['LONGITUDE']:
                longitude = value
            elif measurement_id == self.MEASUREMENT_IDS['BATTERY']:
                battery = value

        # GPS dáta sú validné len ak máme obe súradnice
        if latitude is not None and longitude is not None:
            # Validácia GPS rozsahov
            if not validate_coordinates(latitude, longitude):
                self.stats['invalid_coordinates'] += 1
                self.logger.warning(
                    f"Invalid GPS coordinates: lat={latitude}, lon={longitude}"
                )
                return None

            # Validácia battery
            validated_battery = self._validate_battery(battery)
            if battery is not None and validated_battery is None:
                self.stats['invalid_battery'] += 1

            return {
                'latitude': latitude,
                'longitude': longitude,
                'battery': validated_battery,
                'gps_timestamp': self._parse_timestamp(timestamp_ms),
                'received_at': datetime.now(timezone.utc)
            }

        return None

    def _validate_battery(self, battery: Optional[int]) -> Optional[int]:
        """
        Validuje battery level

        Args:
            battery: Battery level value

        Returns:
            Validated battery level or None
        """
        if battery is None:
            return None

        if self.BATTERY_MIN <= battery <= self.BATTERY_MAX:
            return battery

        self.logger.warning(f"Invalid battery level: {battery} (expected 0-100)")
        return None

    def _parse_timestamp(self, timestamp_ms: Optional[int]) -> Optional[datetime]:
        """
        Konvertuje Unix timestamp (ms) na datetime

        Args:
            timestamp_ms: Unix timestamp v milisekundách

        Returns:
            Datetime object (UTC) or None
        """
        if timestamp_ms is None:
            return None

        try:
            return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        except (ValueError, OSError) as e:
            self.logger.warning(f"Invalid timestamp: {timestamp_ms} - {e}")
            return None

    def _extract_rssi(self, original: dict) -> Optional[int]:
        """
        Extrahuje a validuje RSSI z rxInfo

        Args:
            original: Original message dict

        Returns:
            RSSI value in dBm or None
        """
        rx_info = original.get('rxInfo', [])
        if rx_info and len(rx_info) > 0:
            rssi = rx_info[0].get('rssi')
            if rssi is not None and self.RSSI_MIN <= rssi <= self.RSSI_MAX:
                return rssi
            elif rssi is not None:
                self.logger.debug(f"RSSI out of range: {rssi}")
        return None

    def _extract_snr(self, original: dict) -> Optional[float]:
        """
        Extrahuje a validuje SNR z rxInfo

        Args:
            original: Original message dict

        Returns:
            SNR value in dB or None
        """
        rx_info = original.get('rxInfo', [])
        if rx_info and len(rx_info) > 0:
            snr = rx_info[0].get('loRaSNR')
            if snr is not None and self.SNR_MIN <= snr <= self.SNR_MAX:
                return snr
            elif snr is not None:
                self.logger.debug(f"SNR out of range: {snr}")
        return None

    def is_gps_message(self, payload: str) -> bool:
        """
        Rýchla kontrola či správa obsahuje GPS dáta

        Args:
            payload: JSON payload ako string

        Returns:
            True ak správa obsahuje GPS measurement IDs
        """
        try:
            return (
                    self.MEASUREMENT_IDS['LATITUDE'] in payload and
                    self.MEASUREMENT_IDS['LONGITUDE'] in payload
            )
        except:
            return False

    def get_stats(self) -> Dict[str, int]:
        """
        Vráti parsing štatistiky

        Returns:
            Dict s počtami správ a errorov
        """
        return self.stats.copy()

    def reset_stats(self):
        """Resetuje štatistiky"""
        for key in self.stats:
            self.stats[key] = 0
        self.logger.info("Statistics reset")


# Utility funkcie pre validáciu GPS súradníc
def validate_coordinates(latitude: float, longitude: float) -> bool:
    """
    Validuje GPS súradnice

    Args:
        latitude: Zemepisná šírka (-90 až 90)
        longitude: Zemepisná dĺžka (-180 až 180)

    Returns:
        True ak sú súradnice validné
    """
    return (
            -90 <= latitude <= 90 and
            -180 <= longitude <= 180
    )


def format_coordinates(latitude: float, longitude: float, precision: int = 6) -> str:
    """
    Formátuje súradnice do čitateľného formátu

    Args:
        latitude: Zemepisná šírka
        longitude: Zemepisná dĺžka
        precision: Počet desatinných miest

    Returns:
        Formatted string (napr. "49.821548, 18.161402")
    """
    return f"{latitude:.{precision}f}, {longitude:.{precision}f}"