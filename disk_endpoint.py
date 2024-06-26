from flask import Flask, jsonify, request
import subprocess
import configparser
import os
import re

app = Flask(__name__)

def get_nvme_disk_info():
    output = subprocess.check_output(["sudo", "nvme", "list"]).decode()
    lines = output.split("\n")[2:]  # Skip the header line

    disk_info = []

    for line in lines:
        if not line.strip():  # Skip empty lines
            continue

        columns = line.split()
        name = columns[0].split("/")[-1]
        serial = columns[2]
        model = " ".join(columns[3:5])  # Capture the full model name

        smartctl_output = subprocess.check_output(["sudo", "smartctl", "-H", "/dev/" + name]).decode()
        health_status = "PASSED" if "PASSED" in smartctl_output else "FAILED"

        disk_info.append({
            "name": name,
            "model": model,
            "serial": serial,
            "health_status": health_status
        })

    return disk_info

def get_other_disk_info():
    command = ["ls", "/dev"]
    output = subprocess.check_output(command).decode()
    drives = re.findall(r"sd[a-z]", output)

    disk_info = []

    for drive in drives:
        command = ["sudo", "smartctl", "-a", f"/dev/{drive}"]
        try:
            output = subprocess.check_output(command).decode()
        except subprocess.CalledProcessError:
            continue

        model = re.search(r"Device Model:\s*(.*)", output)
        serial_number = re.search(r"Serial Number:\s*(.*)", output)
        health_status = re.search(r"SMART overall-health self-assessment test result:\s*(.*)", output)

        if model and serial_number and health_status:
            disk_info.append({
                "name": drive,
                "model": model.group(1),
                "serial": serial_number.group(1),
                "health_status": health_status.group(1)
            })

    return disk_info

@app.route('/disk_info', methods=['GET'])
def get_disk_info():
    config = read_config()
    token = config.get('DEFAULT', 'TOKEN')

    # Input validation
    if not request.args.get('token') == token:
        return jsonify({"error": "Invalid token"}), 403

    nvme_disk_info = get_nvme_disk_info()
    other_disk_info = get_other_disk_info()

    # Combine the disk info and remove duplicates
    combined_disk_info = nvme_disk_info + other_disk_info
    unique_disk_info = [dict(t) for t in set(tuple(d.items()) for d in combined_disk_info)]

    # Create a dictionary where each key is a disk name and the value is the disk info
    disk_info_dict = {disk['name']: disk for disk in unique_disk_info}

    return jsonify(disk_info_dict)

@app.route('/disk_info/<disk_name>', methods=['GET'])
def get_specific_disk_info(disk_name):
    config = read_config()
    token = config.get('DEFAULT', 'TOKEN')

    # Input validation
    if not request.args.get('token') == token:
        return jsonify({"error": "Invalid token"}), 403

    nvme_disk_info = get_nvme_disk_info()
    other_disk_info = get_other_disk_info()

    # Combine the disk info and remove duplicates
    combined_disk_info = nvme_disk_info + other_disk_info
    unique_disk_info = [dict(t) for t in set(tuple(d.items()) for d in combined_disk_info)]

    # Create a dictionary where each key is a disk name and the value is the disk info
    disk_info_dict = {disk['name']: disk for disk in unique_disk_info}

    # Return the info of the specific disk
    if disk_name in disk_info_dict:
        return jsonify(disk_info_dict[disk_name])
    else:
        return jsonify({"error": "Disk not found"}), 404

def read_config():
    config = configparser.ConfigParser()
    dir_path = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(dir_path, 'disk_endpoint.conf')
    config.read(config_path)
    return config

def run_app():
    config = read_config()
    host = config.get('DEFAULT', 'HOST')
    port = config.getint('DEFAULT', 'PORT')
    use_https = config.getboolean('DEFAULT', 'USE_HTTPS')
    certificate_path = config.get('DEFAULT', 'CERTIFICATE_PATH')
    key_path = config.get('DEFAULT', 'KEY_PATH')

    if use_https:
        if os.path.exists(certificate_path) and os.path.exists(key_path):
            app.run(host=host, port=port, ssl_context=(certificate_path, key_path))
        else:
            app.run(host=host, port=port)
    else:
        app.run(host=host, port=port)

if __name__ == '__main__':
    run_app()
