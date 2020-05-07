# -*- coding: utf-8 -*-
# Copyright (c) 2014 Jim Kemp <kemp.jim@gmail.com>
# Copyright (c) 2017 Gene Liverman <gene@technicalissues.us>
# Distributed under the MIT License (https://opensource.org/licenses/MIT)

# standard imports
import datetime
import os
import platform
import signal
import sys
import time
import json
import logging
import logging.handlers

# third party imports
from darksky import forecast
import pygame
import requests

from piweatherrock import utils

# globals
UNICODE_DEGREE = u'\xb0'


def exit_gracefully(signum, frame):
    sys.exit(0)


signal.signal(signal.SIGTERM, exit_gracefully)


###############################################################################
class Weather:
    """ Fetches weather reports from Dark Sky for displaying on a screen. """
    screen = None

    ####################################################################
    def __init__(self, config_file):
        with open(config_file, "r") as f:
            self.config = json.load(f)

        # Initialize logger
        self.log = self.get_logger()
        "Ininitializes a new pygame screen using the framebuffer"
        if platform.system() == 'Darwin':
            pygame.display.init()
            driver = pygame.display.get_driver()
            self.log.debug(f"Using the {driver} driver.")
        else:
            # Based on "Python GUI in Linux frame buffer"
            # http://www.karoltomala.com/blog/?p=679
            disp_no = os.getenv("DISPLAY")
            if disp_no:
                self.log.debug(f"X Display = {disp_no}")

            # Check which frame buffer drivers are available
            # Start with fbcon since directfb hangs with composite output
            drivers = ['x11', 'fbcon', 'directfb', 'svgalib']
            found = False
            for driver in drivers:
                # Make sure that SDL_VIDEODRIVER is set
                if not os.getenv('SDL_VIDEODRIVER'):
                    os.putenv('SDL_VIDEODRIVER', driver)
                try:
                    pygame.display.init()
                except pygame.error:
                    self.log.debug("Driver: {driver} failed.")
                    continue
                found = True
                break

            if not found:
                self.log.exception("No suitable video driver found!")

        size = (pygame.display.Info().current_w,
                pygame.display.Info().current_h)
        self.log.debug(f"Framebuffer Size: {size[0]} x {size[1]}")
        self.screen = pygame.display.set_mode(size, pygame.FULLSCREEN)
        # Clear the screen to start
        self.screen.fill((0, 0, 0))
        # Initialise font support
        pygame.font.init()
        # Render the screen
        pygame.mouse.set_visible(0)
        pygame.display.update()

        if self.config["fullscreen"]:
            self.xmax = pygame.display.Info().current_w - 35
            self.ymax = pygame.display.Info().current_h - 5
            if self.xmax <= 1024:
                self.icon_size = '64'
            else:
                self.icon_size = '256'
        else:
            self.xmax = 480 - 35
            self.ymax = 320 - 5
            self.icon_size = '64'
        self.subwindow_text_height = 0.055
        self.time_date_text_height = 0.115
        self.time_date_small_text_height = 0.075
        self.time_date_y_position = 8
        self.time_date_small_y_position = 18

        self.last_update_check = 0

    def deg_to_compass(self, degrees):
        val = int((degrees/22.5)+.5)
        dirs = ["N", "NNE", "NE", "ENE",
                "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW",
                "W", "WNW", "NW", "NNW"]
        return dirs[(val % 16)]

    def units_decoder(self, units):
        """
        https://darksky.net/dev/docs has lists out what each
        unit is. The method below is just a codified version
        of what is on that page.
        """
        si_dict = {
            'nearestStormDistance': 'Kilometers',
            'precipIntensity': 'Millimeters per hour',
            'precipIntensityMax': 'Millimeters per hour',
            'precipAccumulation': 'Centimeters',
            'temperature': 'Degrees Celsius',
            'temperatureMin': 'Degrees Celsius',
            'temperatureMax': 'Degrees Celsius',
            'apparentTemperature': 'Degrees Celsius',
            'dewPoint': 'Degrees Celsius',
            'windSpeed': 'Meters per second',
            'windGust': 'Meters per second',
            'pressure': 'Hectopascals',
            'visibility': 'Kilometers',
        }
        ca_dict = si_dict.copy()
        ca_dict['windSpeed'] = 'Kilometers per hour'
        ca_dict['windGust'] = 'Kilometers per hour'
        uk2_dict = si_dict.copy()
        uk2_dict['nearestStormDistance'] = 'Miles'
        uk2_dict['visibility'] = 'Miles'
        uk2_dict['windSpeed'] = 'Miles per hour'
        uk2_dict['windGust'] = 'Miles per hour'
        us_dict = {
            'nearestStormDistance': 'Miles',
            'precipIntensity': 'Inches per hour',
            'precipIntensityMax': 'Inches per hour',
            'precipAccumulation': 'Inches',
            'temperature': 'Degrees Fahrenheit',
            'temperatureMin': 'Degrees Fahrenheit',
            'temperatureMax': 'Degrees Fahrenheit',
            'apparentTemperature': 'Degrees Fahrenheit',
            'dewPoint': 'Degrees Fahrenheit',
            'windSpeed': 'Miles per hour',
            'windGust': 'Miles per hour',
            'pressure': 'Millibars',
            'visibility': 'Miles',
        }
        switcher = {
            'ca': ca_dict,
            'uk2': uk2_dict,
            'us': us_dict,
            'si': si_dict,
        }
        return switcher.get(units, "Invalid unit name")

    def get_abbreviation(self, phrase):
        abbreviation = ''.join(item[0].lower() for item in phrase.split())
        return abbreviation

    def get_windspeed_abbreviation(self, unit):
        return self.get_abbreviation(self.units_decoder(unit)['windSpeed'])

    def get_temperature_letter(self, unit):
        return self.units_decoder(unit)['temperature'].split(' ')[-1][0].upper()

    def icon_mapping(self, icon, size):
        """
        https://darksky.net/dev/docs has this to say about icons:
        icon optional
        A machine-readable text summary of this data point, suitable for
        selecting an icon for display. If defined, this property will have one
        of the following values: clear-day, clear-night, rain, snow, sleet,
        wind, fog, cloudy, partly-cloudy-day, or partly-cloudy-night.
        (Developers should ensure that a sensible default is defined, as
        additional values, such as hail, thunderstorm, or tornado, may be
        defined in the future.)

        Based on that, this method will map the Dark Sky icon name to the name
        of an icon in this project.
        """
        if icon == 'clear-day':
            icon_path = 'icons/{}/clear.png'.format(size)
        elif icon == 'clear-night':
            icon_path = 'icons/{}/nt_clear.png'.format(size)
        elif icon == 'rain':
            icon_path = 'icons/{}/rain.png'.format(size)
        elif icon == 'snow':
            icon_path = 'icons/{}/snow.png'.format(size)
        elif icon == 'sleet':
            icon_path = 'icons/{}/sleet.png'.format(size)
        elif icon == 'wind':
            icon_path = 'icons/alt_icons/{}/wind.png'.format(size)
        elif icon == 'fog':
            icon_path = 'icons/{}/fog.png'.format(size)
        elif icon == 'cloudy':
            icon_path = 'icons/{}/cloudy.png'.format(size)
        elif icon == 'partly-cloudy-day':
            icon_path = 'icons/{}/partlycloudy.png'.format(size)
        elif icon == 'partly-cloudy-night':
            icon_path = 'icons/{}/nt_partlycloudy.png'.format(size)
        else:
            icon_path = 'icons/{}/unknown.png'.format(size)

        return os.path.join(os.path.dirname(__file__), icon_path)

    # Create logger
    def get_logger(self):
        lvl_str = f"logging.{self.config['log_level']}"
        log = logging.getLogger()
        log.setLevel(eval(lvl_str))
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(message)s",
            datefmt='%Y-%m-%d %H:%M:%S')
        handler = logging.handlers.RotatingFileHandler(
                ".log", maxBytes=500000, backupCount=3)
        if (log.hasHandlers()):
            log.handlers.clear()
        handler.setFormatter(formatter)
        log.addHandler(handler)

        return log

    def __del__(self):
        "Destructor to make sure pygame shuts down, etc."

    def get_forecast(self):
        if (time.time() - self.last_update_check) > self.config["update_freq"]:
            self.last_update_check = time.time()
            try:
                self.weather = forecast(
                    self.config["ds_api_key"],
                    self.config["lat"],
                    self.config["lon"],
                    exclude='minutely',
                    units=self.config["units"],
                    lang=self.config["lang"])

                sunset_today = datetime.datetime.fromtimestamp(
                    self.weather.daily[0].sunsetTime)
                if datetime.datetime.now() < sunset_today:
                    index = 0
                    sr_suffix = 'today'
                    ss_suffix = 'tonight'
                else:
                    index = 1
                    sr_suffix = 'tomorrow'
                    ss_suffix = 'tomorrow'

                self.sunrise = self.weather.daily[index].sunriseTime
                self.sunrise_string = datetime.datetime.fromtimestamp(
                    self.sunrise).strftime("%I:%M %p {}").format(sr_suffix)
                self.sunset = self.weather.daily[index].sunsetTime
                self.sunset_string = datetime.datetime.fromtimestamp(
                    self.sunset).strftime("%I:%M %p {}").format(ss_suffix)

                # start with saying we don't need an umbrella
                self.take_umbrella = False
                icon_now = self.weather.icon
                icon_today = self.weather.daily[0].icon
                if icon_now == 'rain' or icon_today == 'rain':
                    self.take_umbrella = True
                else:
                    # determine if an umbrella is needed during daylight hours
                    curr_date = datetime.datetime.today().date()
                    for hour in self.weather.hourly:
                        hr = datetime.datetime.fromtimestamp(hour.time)
                        sr = datetime.datetime.fromtimestamp(
                            self.weather.daily[0].sunriseTime)
                        ss = datetime.datetime.fromtimestamp(
                            self.weather.daily[0].sunsetTime)
                        rain_chance = hour.precipProbability
                        is_today = hr.date() == curr_date
                        is_daylight_hr = hr >= sr and hr <= ss
                        if is_today and is_daylight_hr and rain_chance >= .25:
                            self.take_umbrella = True
                            break

            except requests.exceptions.RequestException as e:
                self.log.exception(f"Request exception: {e}")
                return False
            except AttributeError as e:
                self.log.exception(f"Attribute error: {e}")
                return False
        return True

    def display_conditions_line(self, label, cond, is_temp, multiplier=None):
        y_start_position = 0.17
        line_spacing_gap = 0.065
        conditions_text_height = 0.05
        degree_symbol_height = 0.03
        degree_symbol_y_offset = 0.001
        x_start_position = 0.52
        second_column_x_start_position = 0.69
        text_color = (255, 255, 255)
        font_name = "freesans"

        if multiplier is None:
            y_start = y_start_position
        else:
            y_start = (y_start_position + line_spacing_gap * multiplier)

        conditions_font = pygame.font.SysFont(
            font_name, int(self.ymax * conditions_text_height), bold=1)

        txt = conditions_font.render(str(label), True, text_color)

        self.screen.blit(
            txt, (self.xmax * x_start_position, self.ymax * y_start))

        txt = conditions_font.render(str(cond), True, text_color)
        self.screen.blit(txt, (self.xmax * second_column_x_start_position,
                               self.ymax * y_start))

        if is_temp:
            txt_x = txt.get_size()[0]
            degree_font = pygame.font.SysFont(
                font_name, int(self.ymax * degree_symbol_height), bold=1)
            degree_txt = degree_font.render(UNICODE_DEGREE, True, text_color)
            self.screen.blit(degree_txt, (
                self.xmax * second_column_x_start_position + txt_x * 1.01,
                self.ymax * (y_start + degree_symbol_y_offset)))
            degree_letter = conditions_font.render(
                self.get_temperature_letter(self.config["units"]),
                True, text_color)
            degree_letter_x = degree_letter.get_size()[0]
            self.screen.blit(degree_letter, (
                self.xmax * second_column_x_start_position +
                txt_x + degree_letter_x * 1.01,
                self.ymax * (y_start + degree_symbol_y_offset)))

    def display_subwindow(self, data, day, c_times):
        subwindow_centers = 0.125
        subwindows_y_start_position = 0.530
        line_spacing_gap = 0.065
        rain_percent_line_offset = 5.95
        rain_present_text_height = 0.060
        text_color = (255, 255, 255)
        font_name = "freesans"

        forecast_font = pygame.font.SysFont(
            font_name, int(self.ymax * self.subwindow_text_height), bold=1)
        rpfont = pygame.font.SysFont(
            font_name, int(self.ymax * rain_present_text_height), bold=1)

        txt = forecast_font.render(day, True, text_color)
        (txt_x, txt_y) = txt.get_size()
        self.screen.blit(txt, (self.xmax *
                               (subwindow_centers * c_times) - txt_x / 2,
                               self.ymax * (subwindows_y_start_position +
                                            line_spacing_gap * 0)))
        if hasattr(data, 'temperatureLow'):
            txt = forecast_font.render(
                str(int(round(data.temperatureLow))) +
                UNICODE_DEGREE +
                ' / ' +
                str(int(round(data.temperatureHigh))) +
                UNICODE_DEGREE + self.get_temperature_letter(
                    self.config["units"]),
                True, text_color)
        else:
            txt = forecast_font.render(
                str(int(round(data.temperature))) +
                UNICODE_DEGREE + self.get_temperature_letter(
                    self.config["units"]),
                True, text_color)
        (txt_x, txt_y) = txt.get_size()
        self.screen.blit(txt, (self.xmax *
                               (subwindow_centers * c_times) - txt_x / 2,
                               self.ymax * (subwindows_y_start_position +
                                            line_spacing_gap * 5)))
        rptxt = rpfont.render(
            str(int(round(data.precipProbability * 100))) + '%',
            True, text_color)
        (txt_x, txt_y) = rptxt.get_size()
        self.screen.blit(rptxt, (self.xmax *
                                 (subwindow_centers * c_times) - txt_x / 2,
                                 self.ymax * (subwindows_y_start_position +
                                              line_spacing_gap *
                                              rain_percent_line_offset)))
        icon = pygame.image.load(
            self.icon_mapping(data.icon, self.icon_size)).convert_alpha()
        (icon_size_x, icon_size_y) = icon.get_size()
        if icon_size_y < 90:
            icon_y_offset = (90 - icon_size_y) / 2
        else:
            icon_y_offset = self.config["icon_offset"]

        self.screen.blit(icon, (self.xmax *
                                (subwindow_centers * c_times) -
                                icon_size_x / 2,
                                self.ymax *
                                (subwindows_y_start_position +
                                 line_spacing_gap
                                 * 1.2) + icon_y_offset))

    def disp_summary(self):
        y_start_position = 0.444
        conditions_text_height = 0.04
        text_color = (255, 255, 255)
        font_name = "freesans"

        conditions_font = pygame.font.SysFont(
            font_name, int(self.ymax * conditions_text_height), bold=1)
        txt = conditions_font.render(self.weather.summary, True, text_color)
        txt_x = txt.get_size()[0]
        x = self.xmax * 0.27 - (txt_x * 1.02) / 2
        self.screen.blit(txt, (x, self.ymax * y_start_position))

    def disp_umbrella_info(self, umbrella_txt):
        x_start_position = 0.52
        y_start_position = 0.444
        conditions_text_height = 0.04
        text_color = (255, 255, 255)
        font_name = "freesans"

        conditions_font = pygame.font.SysFont(
            font_name, int(self.ymax * conditions_text_height), bold=1)
        txt = conditions_font.render(umbrella_txt, True, text_color)
        self.screen.blit(txt, (
            self.xmax * x_start_position,
            self.ymax * y_start_position))

    def disp_weather(self):
        # Fill the screen with black
        self.screen.fill((0, 0, 0))
        xmin = 10
        lines = 5
        line_color = (255, 255, 255)
        text_color = (255, 255, 255)
        font_name = "freesans"

        self.draw_screen_border(line_color, xmin, lines)
        self.disp_time_date(font_name, text_color)
        self.disp_current_temp(font_name, text_color)
        self.disp_summary()
        self.display_conditions_line(
            'Feels Like:', int(round(self.weather.apparentTemperature)),
            True)

        try:
            wind_bearing = self.weather.windBearing
            wind_direction = self.deg_to_compass(wind_bearing) + ' @ '
        except AttributeError:
            wind_direction = ''
        wind_txt = wind_direction + str(
            int(round(self.weather.windSpeed))) + \
            ' ' + self.get_windspeed_abbreviation(self.config["units"])
        self.display_conditions_line(
            'Wind:', wind_txt, False, 1)

        self.display_conditions_line(
            'Humidity:', str(int(round((self.weather.humidity * 100)))) + '%',
            False, 2)

        # Skipping multiplier 3 (line 4)

        if self.take_umbrella:
            umbrella_txt = 'Grab your umbrella!'
        else:
            umbrella_txt = 'No umbrella needed today.'
        self.disp_umbrella_info(umbrella_txt)

        # Today
        today = self.weather.daily[0]
        today_string = "Today"
        multiplier = 1
        self.display_subwindow(today, today_string, multiplier)

        # counts from 0 to 2
        for future_day in range(3):
            this_day = self.weather.daily[future_day + 1]
            this_day_no = datetime.datetime.fromtimestamp(this_day.time)
            this_day_string = this_day_no.strftime("%A")
            multiplier += 2
            self.display_subwindow(this_day, this_day_string, multiplier)

        # Update the display
        pygame.display.update()

    def disp_hourly(self):
        # Fill the screen with black
        self.screen.fill((0, 0, 0))
        xmin = 10
        lines = 5
        line_color = (255, 255, 255)
        text_color = (255, 255, 255)
        font_name = "freesans"

        self.draw_screen_border(line_color, xmin, lines)
        self.disp_time_date(font_name, text_color)
        self.disp_current_temp(font_name, text_color)
        self.disp_summary()
        self.display_conditions_line(
            'Feels Like:', int(round(self.weather.apparentTemperature)),
            True)

        try:
            wind_bearing = self.weather.windBearing
            wind_direction = self.deg_to_compass(wind_bearing) + ' @ '
        except AttributeError:
            wind_direction = ''
        wind_txt = wind_direction + str(
            int(round(self.weather.windSpeed))) + \
            ' ' + self.get_windspeed_abbreviation(self.config["units"])
        self.display_conditions_line(
            'Wind:', wind_txt, False, 1)

        self.display_conditions_line(
            'Humidity:', str(int(round((self.weather.humidity * 100)))) + '%',
            False, 2)

        # Skipping multiplier 3 (line 4)

        if self.take_umbrella:
            umbrella_txt = 'Grab your umbrella!'
        else:
            umbrella_txt = 'No umbrella needed today.'
        self.disp_umbrella_info(umbrella_txt)

        # Current hour
        this_hour = self.weather.hourly[0]
        this_hour_24_int = int(datetime.datetime.fromtimestamp(
            this_hour.time).strftime("%H"))
        if this_hour_24_int <= 11:
            ampm = 'a.m.'
        else:
            ampm = 'p.m.'
        this_hour_12_int = int(datetime.datetime.fromtimestamp(
            this_hour.time).strftime("%I"))
        this_hour_string = "{} {}".format(str(this_hour_12_int), ampm)
        multiplier = 1
        self.display_subwindow(this_hour, this_hour_string, multiplier)

        # counts from 0 to 2
        for future_hour in range(3):
            this_hour = self.weather.hourly[future_hour + 1]
            this_hour_24_int = int(datetime.datetime.fromtimestamp(
                this_hour.time).strftime("%H"))
            if this_hour_24_int <= 11:
                ampm = 'a.m.'
            else:
                ampm = 'p.m.'
            this_hour_12_int = int(datetime.datetime.fromtimestamp(
                this_hour.time).strftime("%I"))
            this_hour_string = "{} {}".format(str(this_hour_12_int), ampm)
            multiplier += 2
            self.display_subwindow(this_hour, this_hour_string, multiplier)

        # Update the display
        pygame.display.update()

    def disp_current_temp(self, font_name, text_color):
        # Outside Temp
        outside_temp_font = pygame.font.SysFont(
            font_name, int(self.ymax * (0.5 - 0.15) * 0.6), bold=1)
        txt = outside_temp_font.render(
            str(int(round(self.weather.temperature))), True, text_color)
        (txt_x, txt_y) = txt.get_size()
        degree_font = pygame.font.SysFont(
            font_name, int(self.ymax * (0.5 - 0.15) * 0.3), bold=1)
        degree_txt = degree_font.render(UNICODE_DEGREE, True, text_color)
        (rendered_am_pm_x, rendered_am_pm_y) = degree_txt.get_size()
        degree_letter = outside_temp_font.render(
            self.get_temperature_letter(self.config["units"]),
            True, text_color)
        (degree_letter_x, degree_letter_y) = degree_letter.get_size()
        # Position text
        x = self.xmax * 0.27 - (txt_x * 1.02 + rendered_am_pm_x +
                                degree_letter_x) / 2
        self.screen.blit(txt, (x, self.ymax * 0.20))
        x = x + (txt_x * 1.02)
        self.screen.blit(degree_txt, (x, self.ymax * 0.2))
        x = x + (rendered_am_pm_x * 1.02)
        self.screen.blit(degree_letter, (x, self.ymax * 0.2))

    def disp_time_date(self, font_name, text_color):
        # Time & Date
        time_date_font = pygame.font.SysFont(
            font_name, int(self.ymax * self.time_date_text_height), bold=1)
        # Small Font for Seconds
        small_font = pygame.font.SysFont(
            font_name,
            int(self.ymax * self.time_date_small_text_height), bold=1)

        time_string = time.strftime("%a, %b %d   %I:%M", time.localtime())
        am_pm_string = time.strftime(" %p", time.localtime())

        rendered_time_string = time_date_font.render(time_string, True,
                                                     text_color)
        (rendered_time_x, rendered_time_y) = rendered_time_string.get_size()
        rendered_am_pm_string = small_font.render(am_pm_string, True,
                                                  text_color)
        (rendered_am_pm_x, rendered_am_pm_y) = rendered_am_pm_string.get_size()

        full_time_string_x_position = self.xmax / 2 - (rendered_time_x +
                                                       rendered_am_pm_x) / 2
        self.screen.blit(rendered_time_string, (full_time_string_x_position,
                                                self.time_date_y_position))
        self.screen.blit(rendered_am_pm_string,
                         (full_time_string_x_position + rendered_time_x + 3,
                          self.time_date_small_y_position))

    def draw_screen_border(self, line_color, xmin, lines):
        # Draw Screen Border
        # Top
        pygame.draw.line(self.screen, line_color, (xmin, 0), (self.xmax, 0),
                         lines)
        # Left
        pygame.draw.line(self.screen, line_color, (xmin, 0),
                         (xmin, self.ymax), lines)
        # Bottom
        pygame.draw.line(self.screen, line_color, (xmin, self.ymax),
                         (self.xmax, self.ymax), lines)
        # Right
        pygame.draw.line(self.screen, line_color, (self.xmax, 0),
                         (self.xmax, self.ymax + 2), lines)
        # Bottom of top box
        pygame.draw.line(self.screen, line_color, (xmin, self.ymax * 0.15),
                         (self.xmax, self.ymax * 0.15), lines)
        # Bottom of middle box
        pygame.draw.line(self.screen, line_color, (xmin, self.ymax * 0.5),
                         (self.xmax, self.ymax * 0.5), lines)
        # Bottom row, left vertical
        pygame.draw.line(self.screen, line_color, (self.xmax * 0.25,
                                                   self.ymax * 0.5),
                         (self.xmax * 0.25, self.ymax), lines)
        # Bottom row, center vertical
        pygame.draw.line(self.screen, line_color, (self.xmax * 0.5,
                                                   self.ymax * 0.15),
                         (self.xmax * 0.5, self.ymax), lines)
        # Bottom row, right vertical
        pygame.draw.line(self.screen, line_color, (self.xmax * 0.75,
                                                   self.ymax * 0.5),
                         (self.xmax * 0.75, self.ymax), lines)

    ####################################################################
    def sPrint(self, text, font, x, line_number, text_color):
        rendered_font = font.render(text, True, text_color)
        self.screen.blit(rendered_font, (x, self.ymax * 0.075 * line_number))

    ####################################################################
    def disp_info(self, in_daylight, day_hrs, day_mins, seconds_til_daylight,
                  delta_seconds_til_dark):
        # Fill the screen with black
        self.screen.fill((0, 0, 0))
        xmin = 10
        lines = 5
        line_color = (0, 0, 0)
        text_color = (255, 255, 255)
        font_name = "freesans"

        # Draw Screen Border
        pygame.draw.line(self.screen, line_color,
                         (xmin, 0), (self.xmax, 0), lines)
        pygame.draw.line(self.screen, line_color,
                         (xmin, 0), (xmin, self.ymax), lines)
        pygame.draw.line(self.screen, line_color,
                         (xmin, self.ymax), (self.xmax, self.ymax), lines)
        pygame.draw.line(self.screen, line_color,
                         (self.xmax, 0), (self.xmax, self.ymax), lines)
        pygame.draw.line(self.screen, line_color,
                         (xmin, self.ymax * 0.15),
                         (self.xmax, self.ymax * 0.15), lines)

        time_height_large = self.time_date_text_height
        time_height_small = self.time_date_small_text_height

        # Time & Date
        regular_font = pygame.font.SysFont(
            font_name, int(self.ymax * time_height_large), bold=1)
        small_font = pygame.font.SysFont(
            font_name, int(self.ymax * time_height_small), bold=1)

        hours_and_minutes = time.strftime("%I:%M", time.localtime())
        am_pm = time.strftime(" %p", time.localtime())

        rendered_hours_and_minutes = regular_font.render(
            hours_and_minutes, True, text_color)
        (tx1, ty1) = rendered_hours_and_minutes.get_size()
        rendered_am_pm = small_font.render(am_pm, True, text_color)
        (tx2, ty2) = rendered_am_pm.get_size()

        tp = self.xmax / 2 - (tx1 + tx2) / 2
        self.screen.blit(rendered_hours_and_minutes,
                         (tp, self.time_date_y_position))
        self.screen.blit(rendered_am_pm,
                         (tp + tx1 + 3, self.time_date_small_y_position))

        self.sPrint("A weather rock powered by Dark Sky", small_font,
                    self.xmax * 0.05, 3, text_color)

        self.sPrint("Sunrise: %s" % self.sunrise_string,
                    small_font, self.xmax * 0.05, 4, text_color)

        self.sPrint("Sunset:  %s" % self.sunset_string,
                    small_font, self.xmax * 0.05, 5, text_color)

        text = "Daylight: %d hrs %02d min" % (day_hrs, day_mins)
        self.sPrint(text, small_font, self.xmax * 0.05, 6, text_color)

        # leaving row 7 blank

        if in_daylight:
            text = "Sunset in %d hrs %02d min" % utils.stot(
                delta_seconds_til_dark)
        else:
            text = "Sunrise in %d hrs %02d min" % utils.stot(
                seconds_til_daylight)
        self.sPrint(text, small_font, self.xmax * 0.05, 8, text_color)

        # leaving row 9 blank

        text = "Weather checked at"
        self.sPrint(text, small_font, self.xmax * 0.05, 10, text_color)

        text = "    %s" % time.strftime(
            "%I:%M:%S %p %Z on %a. %d %b %Y ",
            time.localtime(self.last_update_check))
        self.sPrint(text, small_font, self.xmax * 0.05, 11, text_color)

        # Update the display
        pygame.display.update()

    # Save a jpg image of the screen.
    ####################################################################
    def screen_cap(self):
        pygame.image.save(self.screen, "screenshot.jpeg")
        self.log.info("Screen capture complete.")